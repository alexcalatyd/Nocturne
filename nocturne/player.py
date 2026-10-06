"""Control de reproducción sobre ffplay.

ffplay se lanza sin ventana y escribe en stderr su reloj maestro (la posición
audible real) cada ~30 ms: ése es el cronómetro que usa toda la interfaz.
La pausa se hace con SIGSTOP/SIGCONT; el salto y el cambio de volumen
relanzan el proceso desde la posición deseada.
"""
from __future__ import annotations

import re
import signal
import subprocess
import threading
import time

STOPPED, PLAYING, PAUSED = "stopped", "playing", "paused"

_CLOCK_RE = re.compile(r"(-?\d+\.\d+)\s+[MAV]-[AV]:")
VOLUME_DEBOUNCE = 0.30      # s sin tocar el volumen antes de relanzar
SPAWN_GRACE = 1.5           # s de cortesía antes de dar por acabada una pista


class Player:
    """Reproductor de una sola pista, con salto, pausa y volumen."""

    def __init__(self, volume: int = 80, sync_offset: float = 0.0) -> None:
        self.state = STOPPED
        self.path: str | None = None
        self.duration: float = 0.0
        self.volume = max(0, min(100, volume))
        self.sync_offset = sync_offset
        self.error: str | None = None

        self._proc: subprocess.Popen | None = None
        self._gen = 0                  # identifica el proceso vivo
        self._lock = threading.Lock()
        self._start_at = 0.0           # offset con el que se lanzó ffplay
        self._clock = 0.0              # último reloj reportado
        self._clock_at = 0.0           # monotonic de ese reporte
        self._has_clock = False
        self._spawned_at = 0.0
        self._pause_pos = 0.0
        self._paused_at = 0.0
        self._drift = 0.0              # tiempo en pausa que ffplay sí contabiliza
        self._pending_volume: int | None = None
        self._volume_at = 0.0
        self._muted_from: int | None = None

    # -- estado ------------------------------------------------------------
    @property
    def is_active(self) -> bool:
        return self.state in (PLAYING, PAUSED)

    @property
    def position(self) -> float:
        """Posición audible en segundos."""
        with self._lock:
            if self.state == PAUSED:
                pos = self._pause_pos
            elif self.state == STOPPED:
                pos = 0.0
            elif self._has_clock:
                pos = self._clock + (time.monotonic() - self._clock_at)
            else:
                pos = self._start_at
        pos += self.sync_offset
        if pos < 0.0:
            return 0.0
        if self.duration and pos > self.duration:
            return self.duration
        return pos

    @property
    def progress(self) -> float:
        return self.position / self.duration if self.duration > 0 else 0.0

    # -- transporte --------------------------------------------------------
    def load(self, path: str, duration: float = 0.0, start: float = 0.0) -> None:
        self.stop()
        self.path = path
        self.duration = max(0.0, duration)
        self.error = None
        self._spawn(start)

    def stop(self) -> None:
        self._kill()
        self.state = STOPPED
        with self._lock:
            self._has_clock = False
            self._start_at = 0.0
            self._pause_pos = 0.0

    def toggle_pause(self) -> None:
        if self.state == PLAYING:
            self.pause()
        elif self.state == PAUSED:
            self.resume()

    def pause(self) -> None:
        if self.state != PLAYING or self._proc is None:
            return
        pos = self.position - self.sync_offset
        try:
            self._proc.send_signal(signal.SIGSTOP)
        except (OSError, ProcessLookupError):
            return
        with self._lock:
            self._pause_pos = pos
            self._paused_at = time.monotonic()
        self.state = PAUSED

    def resume(self) -> None:
        if self.state != PAUSED or self._proc is None:
            return
        try:
            self._proc.send_signal(signal.SIGCONT)
        except (OSError, ProcessLookupError):
            # El proceso murió mientras estaba parado: relanza desde donde iba.
            self._spawn(self._pause_pos)
            return
        with self._lock:
            now = time.monotonic()
            # ffplay deriva su reloj del reloj de pared, así que al volver de
            # SIGSTOP va adelantado justo lo que duró la pausa: se descuenta.
            self._drift += now - self._paused_at
            self._clock = self._pause_pos
            self._clock_at = now
            self._has_clock = True
        self.state = PLAYING

    def seek_to(self, position: float) -> None:
        if not self.path:
            return
        if self.duration:
            position = max(0.0, min(self.duration - 0.35, position))
        else:
            position = max(0.0, position)
        was_paused = self.state == PAUSED
        self._spawn(position)
        if was_paused:
            # Deja el salto visible pero sin sonar.
            self.pause()

    def seek_relative(self, delta: float) -> None:
        self.seek_to(self.position - self.sync_offset + delta)

    def set_volume(self, value: int) -> None:
        self.volume = max(0, min(100, int(value)))
        self._muted_from = None
        if self.state == PLAYING:
            self._pending_volume = self.volume
            self._volume_at = time.monotonic()

    def change_volume(self, delta: int) -> None:
        self.set_volume(self.volume + delta)

    def toggle_mute(self) -> None:
        if self._muted_from is None:
            previous = self.volume
            self.set_volume(0)
            self._muted_from = previous
        else:
            restore = self._muted_from
            self.set_volume(restore)

    @property
    def muted(self) -> bool:
        return self._muted_from is not None

    # -- bucle -------------------------------------------------------------
    def poll(self) -> bool:
        """Llamar cada frame. Devuelve True si la pista terminó sola."""
        now = time.monotonic()
        if (self._pending_volume is not None
                and now - self._volume_at >= VOLUME_DEBOUNCE):
            self._pending_volume = None
            if self.state == PLAYING:
                self._spawn(self.position - self.sync_offset)
            return False

        proc = self._proc
        if proc is None or self.state != PLAYING:
            return False
        if proc.poll() is None:
            return False
        if now - self._spawned_at < SPAWN_GRACE and not self._has_clock:
            # Murió sin sonar: probablemente un archivo ilegible.
            self.state = STOPPED
            self.error = "ffplay no pudo reproducir el archivo"
            return False
        self.state = STOPPED
        return True

    # -- internos ----------------------------------------------------------
    def _spawn(self, start: float) -> None:
        if not self.path:
            return
        self._kill()
        args = ["ffplay", "-nodisp", "-autoexit", "-hide_banner", "-loglevel", "info",
                "-volume", str(self.volume)]
        if start > 0.05:
            args += ["-ss", f"{start:.3f}"]
        args.append(self.path)
        try:
            proc = subprocess.Popen(
                args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE, start_new_session=True)
        except OSError as exc:
            self.error = f"no se pudo lanzar ffplay: {exc}"
            self.state = STOPPED
            return
        with self._lock:
            self._gen += 1
            gen = self._gen
            self._proc = proc
            self._start_at = start
            self._clock = start
            self._clock_at = time.monotonic()
            self._has_clock = False
            self._drift = 0.0
            self._spawned_at = time.monotonic()
        self.state = PLAYING
        threading.Thread(target=self._read_clock, args=(proc, gen), daemon=True).start()

    def _read_clock(self, proc: subprocess.Popen, gen: int) -> None:
        """Lee el reloj maestro de ffplay desde stderr (líneas con \\r)."""
        stream = proc.stderr
        if stream is None:
            return
        buf = bytearray()
        try:
            while True:
                chunk = stream.read(1)
                if not chunk:
                    break
                if chunk in b"\r\n":
                    if buf:
                        self._consume(bytes(buf), gen)
                        buf.clear()
                else:
                    buf += chunk
                    if len(buf) > 512:
                        buf.clear()
        except (OSError, ValueError):
            pass
        finally:
            try:
                stream.close()
            except OSError:
                pass

    def _consume(self, line: bytes, gen: int) -> None:
        match = _CLOCK_RE.search(line.decode("utf-8", "replace"))
        if not match:
            return
        try:
            value = float(match.group(1))
        except ValueError:
            return
        if value != value or value < 0:      # NaN o basura
            return
        with self._lock:
            if gen != self._gen or self.state != PLAYING:
                return
            self._clock = value - self._drift
            self._clock_at = time.monotonic()
            self._has_clock = True

    def _kill(self) -> None:
        with self._lock:
            proc, self._proc = self._proc, None
            self._gen += 1
            self._has_clock = False
        if proc is None:
            return
        try:
            if proc.poll() is None:
                proc.send_signal(signal.SIGCONT)   # por si estaba pausado
                proc.kill()
                proc.wait(timeout=1.0)
        except (OSError, subprocess.TimeoutExpired, ProcessLookupError):
            pass
