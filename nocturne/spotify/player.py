"""Reproductor de Spotify sobre la API de Connect, con la misma cara que Player.

Nocturne manda la cola entera como lista de URIs. Una sola URI no vale:
librespot no sabe reportar el estado de una cola de un elemento sin contexto,
Spotify lo rechaza con un 400 y la reproducción se corta a los tres segundos.

Con la cola completa, Spotify encadena solo en orden normal y Nocturne le
sigue el paso (`advanced_to`); cuando manda Nocturne —aleatorio, repetición,
salto manual— reordena mandando un play nuevo. El reloj se lleva en local y se
corrige contra /me/player cada pocos segundos, que es lo que deja el rate limit.
"""
from __future__ import annotations

import queue
import threading
import time

from ..player import PAUSED, PLAYING, STOPPED
from .client import ApiError, Client

URI_WINDOW = 80             # pistas que se le mandan a Spotify de una vez
SYNC_EVERY = 2.0            # s entre sincronizaciones con Spotify
SYNC_NEAR_END = 0.8         # s entre sincronizaciones cerca del final
DRIFT_TOLERANCE = 0.9       # s de desfase que se toleran sin dar un salto
VOLUME_DEBOUNCE = 0.45      # s sin tocar el volumen antes de mandarlo
END_MARGIN = 0.35           # s antes del final en que se da por terminada
DEVICE_WAIT = 12.0          # s de margen para que el altavoz propio aparezca


class SpotifyPlayer:
    """Controla el dispositivo Connect activo. API compatible con Player."""

    def __init__(self, client: Client, volume: int = 80,
                 sync_offset: float = 0.0) -> None:
        self.client = client
        self.state = STOPPED
        self.path: str | None = None
        self.duration = 0.0
        self.volume = max(0, min(100, volume))
        self.sync_offset = sync_offset
        self.error: str | None = None
        self.device_name = ""
        self.preferred_device = ""
        self.expect_preferred = False     # ¿lo está arrancando Nocturne?

        self._device_id: str | None = None
        self._lock = threading.Lock()
        self._anchor_pos = 0.0
        self._anchor_at = time.monotonic()
        self._gen = 0
        self._finished = False
        self._muted_from: int | None = None
        self.advanced_to: str | None = None   # Spotify encadenó por su cuenta
        self.advanced_duration = 0.0
        self._queue: list[str] = []
        self._pending_volume: int | None = None
        self._volume_at = 0.0
        self._last_sync = 0.0
        self._quiet_sync_until = 0.0

        self._commands: queue.Queue = queue.Queue()
        self._stop_worker = False
        self._worker = threading.Thread(target=self._run, daemon=True)
        self._worker.start()

    # -- estado ------------------------------------------------------------
    @property
    def is_active(self) -> bool:
        return self.state in (PLAYING, PAUSED)

    @property
    def position(self) -> float:
        with self._lock:
            pos = self._anchor_pos
            if self.state == PLAYING:
                pos += time.monotonic() - self._anchor_at
        pos += self.sync_offset
        if pos < 0.0:
            return 0.0
        if self.duration and pos > self.duration:
            return self.duration
        return pos

    @property
    def progress(self) -> float:
        return self.position / self.duration if self.duration > 0 else 0.0

    @property
    def muted(self) -> bool:
        return self._muted_from is not None

    def _anchor(self, position: float) -> None:
        with self._lock:
            self._anchor_pos = max(0.0, position)
            self._anchor_at = time.monotonic()

    # -- transporte --------------------------------------------------------
    def load(self, path: str, duration: float = 0.0, start: float = 0.0,
             queue: list[str] | None = None, offset: int = 0,
             context: str | None = None) -> None:
        self.path = path
        self.duration = max(0.0, duration)
        self.error = None
        self._finished = False
        self.advanced_to = None
        self._anchor(start)
        self.state = PLAYING
        if context:
            with self._lock:
                self._gen += 1
                gen = self._gen
            self._submit(("play_context", context, max(0, offset), start, gen))
            return

        # Sin contexto (búsquedas): ventana de URIs, que es lo que hay.
        full = queue or [path]
        if path not in full:
            full = [path]
            offset = 0
        start_i = max(0, min(offset, len(full) - 1))
        window = full[start_i:start_i + URI_WINDOW]
        if len(window) < 2:
            window = full[max(0, start_i - 1):start_i + URI_WINDOW] or [path]
        index = window.index(path) if path in window else 0
        with self._lock:
            self._gen += 1
            gen = self._gen
            self._queue = list(window)
        self._submit(("play", window, index, start, gen))

    def step_next(self, path: str, duration: float = 0.0) -> None:
        """Pasa a la pista contigua sin volver a mandar un play.

        Dentro de un contexto librespot ya tiene la siguiente descargada y
        decodificada (reproducción sin huecos), así que `/me/player/next` suena
        casi al instante; un play nuevo tira esa precarga y la vuelve a pedir
        al CDN, que es de dónde salía el retardo al pulsar `n`.
        """
        self.path = path
        self.duration = max(0.0, duration)
        self.error = None
        self._finished = False
        self.advanced_to = None
        self._anchor(0.0)
        self.state = PLAYING
        with self._lock:
            self._gen += 1
        now = time.monotonic()
        self._last_sync = now
        self._quiet_sync_until = now + 2.5
        self._submit(("next",))

    def stop(self) -> None:
        was_active = self.is_active
        self.state = STOPPED
        self.path = None
        self._finished = False
        self._anchor(0.0)
        with self._lock:
            self._gen += 1
        if was_active:
            self._submit(("pause",))

    def toggle_pause(self) -> None:
        if self.state == PLAYING:
            self.pause()
        elif self.state == PAUSED:
            self.resume()

    def pause(self) -> None:
        if self.state != PLAYING:
            return
        self._anchor(self.position - self.sync_offset)
        self.state = PAUSED
        self._submit(("pause",))

    def resume(self) -> None:
        if self.state != PAUSED:
            return
        self._anchor(self._anchor_pos)
        self.state = PLAYING
        self._submit(("resume",))

    def seek_to(self, position: float) -> None:
        if not self.path:
            return
        if self.duration:
            position = max(0.0, min(self.duration - END_MARGIN, position))
        else:
            position = max(0.0, position)
        self._anchor(position)
        self._finished = False
        self._submit(("seek", position))

    def seek_relative(self, delta: float) -> None:
        self.seek_to(self.position - self.sync_offset + delta)

    def set_volume(self, value: int) -> None:
        self.volume = max(0, min(100, int(value)))
        self._muted_from = None
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
            self.set_volume(self._muted_from)

    # -- bucle -------------------------------------------------------------
    def poll(self) -> bool:
        """Llamar cada frame. Devuelve True si la pista terminó sola."""
        now = time.monotonic()
        if (self._pending_volume is not None
                and now - self._volume_at >= VOLUME_DEBOUNCE):
            value, self._pending_volume = self._pending_volume, None
            self._submit(("volume", value))

        if self.state == PLAYING:
            near_end = (self.duration
                        and self.position - self.sync_offset > self.duration - 6.0)
            gap = SYNC_NEAR_END if near_end else SYNC_EVERY
            if now - self._last_sync >= gap:
                self._last_sync = now
                self._submit(("sync",))

        # El final de la cola lo declara _sync: dentro de la cola encadena
        # Spotify y Nocturne se limita a seguirle.
        if self._finished:
            self._finished = False
            self.state = STOPPED
            return True
        return False

    def close(self) -> None:
        self._stop_worker = True
        self._submit(("quit",))

    # -- trabajo de red ----------------------------------------------------
    def _submit(self, command: tuple) -> None:
        self._commands.put(command)

    def _run(self) -> None:
        while not self._stop_worker:
            command = self._commands.get()
            if not command or command[0] == "quit":
                return
            try:
                self._dispatch(command)
            except ApiError as exc:
                self._fail(str(exc))
            except Exception as exc:
                self._fail(f"Spotify: {exc}")

    def _fail(self, message: str) -> None:
        self.error = message
        self.state = STOPPED

    def _dispatch(self, command: tuple) -> None:
        kind = command[0]
        if kind in ("play", "play_context"):
            if kind == "play_context":
                _, context, index, start, gen = command
                device = self._ensure_device()
                self.client.play(context_uri=context, offset=index,
                                 position_ms=int(start * 1000), device_id=device)
            else:
                _, uris, index, start, gen = command
                device = self._ensure_device()
                self.client.play(uris=uris, offset=index,
                                 position_ms=int(start * 1000), device_id=device)
            with self._lock:
                stale = gen != self._gen
            if not stale:
                self._anchor(start)
                now = time.monotonic()
                self._last_sync = now
                # Spotify tarda un poco en reflejar la orden: hasta entonces
                # /me/player sigue contando lo anterior y daría un salto feo.
                self._quiet_sync_until = now + 2.5
        elif kind == "next":
            self.client.skip_next(device_id=self._ensure_device())
        elif kind == "pause":
            self.client.pause(device_id=self._device_id)
        elif kind == "resume":
            self.client.resume(device_id=self._device_id)
        elif kind == "seek":
            self.client.seek(int(command[1] * 1000), device_id=self._device_id)
        elif kind == "volume":
            self.client.volume(command[1], device_id=self._device_id)
        elif kind == "sync":
            self._sync()

    def _ensure_device(self) -> str | None:
        """Elige altavoz, esperando al propio si Nocturne lo está levantando.

        librespot tarda unos segundos en autenticarse y aparecer en Connect;
        mandarle el play antes de eso se traga la orden sin decir nada.
        """
        if self._device_id:
            return self._device_id
        deadline = time.monotonic() + (DEVICE_WAIT if self.expect_preferred else 0.0)
        pick = None
        while True:
            devices = self.client.devices()
            # El altavoz que arranca Nocturne manda sobre cualquier otro.
            pick = next((d for d in devices
                         if d.get("name") == self.preferred_device), None)
            if pick is None and time.monotonic() >= deadline:
                pick = next((d for d in devices if d.get("is_active")), None)
                if pick is None and devices:
                    pick = next((d for d in devices if not d.get("is_restricted")),
                                devices[0])
            if pick is not None:
                break
            if time.monotonic() >= deadline:
                if self.expect_preferred:
                    raise ApiError(
                        "librespot no llegó a registrarse; suele ser la red "
                        "bloqueando el puerto 4070 (prueba otra conexión)")
                raise ApiError(
                    "no hay ningún dispositivo de Spotify a la escucha "
                    "(abre la app o instala librespot)")
            time.sleep(0.7)

        self._device_id = pick.get("id")
        self.device_name = pick.get("name", "")
        return self._device_id

    def use_device(self, device_id: str, name: str = "") -> None:
        self._device_id = device_id
        self.device_name = name

    def _sync(self) -> None:
        """Corrige el reloj local contra lo que dice Spotify de verdad."""
        if time.monotonic() < self._quiet_sync_until:
            return
        state = self.client.playback_state()
        if not state:
            return
        device = state.get("device") or {}
        if device.get("id"):
            self._device_id = device["id"]
            self.device_name = device.get("name", "")
        item = state.get("item") or {}
        remote = float(state.get("progress_ms") or 0) / 1000.0
        playing = bool(state.get("is_playing"))
        uri = item.get("uri")
        if self.path and uri and uri != self.path:
            # Spotify ha encadenado (o alguien cambió desde otro mando).
            self.path = uri
            self.duration = float(item.get("duration_ms") or 0) / 1000.0
            self.advanced_duration = self.duration
            self.advanced_to = uri
            self._anchor(remote)
            self.state = PLAYING if playing else PAUSED
            return
        if playing and self.state == PAUSED:
            self.state = PLAYING
            self._anchor(remote)
            return
        if not playing and self.state == PLAYING:
            if self.duration and remote >= self.duration - 2.0:
                self._finished = True     # se acabó la cola: lo recoge poll()
            else:
                self.state = PAUSED
                self._anchor(remote)
            return
        if abs(remote - (self.position - self.sync_offset)) > DRIFT_TOLERANCE:
            self._anchor(remote)
