"""De dónde salen las muestras cuando quien suena es Spotify.

Dos maneras, según cómo tengas montado el audio:

* `pipe`     — librespot escribe PCM crudo en un FIFO (`--backend pipe`).
               Nocturne lo lee, lo manda a la salida de audio con ffmpeg y de
               camino se queda una copia en mono para el espectro.
* `loopback` — suena la app oficial de Spotify y capturamos el audio del
               sistema con un dispositivo de bucle (BlackHole, Loopback…).

Sin ninguna de las dos, Nocturne sigue funcionando: sólo se queda sin dibujo.
"""
from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import threading

from ..analysis import SAMPLE_RATE, StreamAnalysis

LIBRESPOT_RATE = 44100
LIBRESPOT_CHANNELS = 2
CHUNK = 4096
_DETACHED = {"start_new_session": True}
_DEVICE_RE = re.compile(r"^\[AVFoundation.*?]\s*\[(\d+)]\s+(.*)$")

# Dispositivos de bucle habituales en macOS, en orden de preferencia.
LOOPBACK_HINTS = ("blackhole", "loopback", "soundflower", "multi-output", "aggregate")


def audio_inputs() -> list[tuple[int, str]]:
    """Dispositivos de entrada de AVFoundation: [(índice, nombre)]."""
    if not shutil.which("ffmpeg"):
        return []
    try:
        out = subprocess.run(
            ["ffmpeg", "-hide_banner", "-f", "avfoundation",
             "-list_devices", "true", "-i", ""],
            stdin=subprocess.DEVNULL, capture_output=True, text=True,
            timeout=15, **_DETACHED)
    except Exception:
        return []
    devices: list[tuple[int, str]] = []
    audio_section = False
    for line in (out.stderr or "").splitlines():
        if "AVFoundation audio devices" in line:
            audio_section = True
            continue
        if "AVFoundation video devices" in line:
            audio_section = False
            continue
        if not audio_section:
            continue
        match = _DEVICE_RE.match(line.strip())
        if match:
            devices.append((int(match.group(1)), match.group(2).strip()))
    return devices


def find_loopback() -> tuple[int, str] | None:
    """El primer dispositivo que huela a bucle de audio."""
    for index, name in audio_inputs():
        lowered = name.lower()
        if any(hint in lowered for hint in LOOPBACK_HINTS):
            return index, name
    return None


def make_fifo(path: str) -> str:
    """Crea (o reutiliza) el FIFO por el que librespot escribirá el PCM."""
    if os.path.exists(path):
        if not stat.S_ISFIFO(os.stat(path).st_mode):
            raise OSError(f"{path} existe y no es un FIFO")
        return path
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    os.mkfifo(path, 0o600)
    return path


class PcmPump:
    """Mantiene vivo el ffmpeg que alimenta el espectro."""

    def __init__(self, analysis: StreamAnalysis, mode: str, source: str) -> None:
        self.analysis = analysis
        self.mode = mode                 # "pipe" | "loopback"
        self.source = source             # ruta del FIFO o índice del dispositivo
        self.error: str | None = None
        self._proc: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
        self._fifo_fd: int | None = None
        self._stop = False

    @property
    def alive(self) -> bool:
        """Vivo desde que arranca el hilo, no desde que ffmpeg existe."""
        if self._stop:
            return False
        thread = self._thread
        if thread is not None and thread.is_alive():
            return True
        proc = self._proc
        return proc is not None and proc.poll() is None

    def start(self) -> bool:
        if self._thread is not None and self._thread.is_alive() and not self._stop:
            return True
        if not shutil.which("ffmpeg"):
            self.error = "hace falta ffmpeg para el espectro"
            return False
        previous = self._thread
        if previous is not None and previous.is_alive():
            previous.join(timeout=2.0)   # que el anterior muera antes de relevarlo
        self._stop = False
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop = True
        proc, self._proc = self._proc, None
        if proc is not None and proc.poll() is None:
            try:
                proc.kill()
                proc.wait(timeout=1.0)
            except (OSError, subprocess.TimeoutExpired):
                pass
        fd, self._fifo_fd = self._fifo_fd, None
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        self.analysis.silence()

    # -- internos ----------------------------------------------------------
    def _args(self) -> list[str]:
        analysis_out = ["-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "s16le", "pipe:1"]
        if self.mode == "pipe":
            # Una entrada, dos salidas: los altavoces y el analizador.
            # La entrada es pipe:0 porque el FIFO lo abrimos nosotros (ver _open_fifo).
            return [
                "ffmpeg", "-hide_banner", "-loglevel", "error",
                "-f", "s16le", "-ar", str(LIBRESPOT_RATE),
                "-ac", str(LIBRESPOT_CHANNELS), "-i", "pipe:0",
                "-filter_complex", "[0:a]asplit=2[play][an]",
                "-map", "[play]", "-f", "audiotoolbox", "default",
                "-map", "[an]", *analysis_out,
            ]
        return [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
            "-f", "avfoundation", "-i", f":{self.source}",
            *analysis_out,
        ]

    def _open_fifo(self) -> int | None:
        """Abre el FIFO en lectura-escritura y lo deja abierto.

        Si el FIFO se queda sin ningún lector, la siguiente escritura de
        librespot falla con EPIPE y deja de reproducir. Manteniendo nosotros un
        descriptor abierto en O_RDWR eso no puede pasar, y de paso ffmpeg nunca
        ve un fin de fichero entre pista y pista.
        """
        if self._fifo_fd is not None:
            return self._fifo_fd
        try:
            self._fifo_fd = os.open(self.source, os.O_RDWR | os.O_NONBLOCK)
            os.set_blocking(self._fifo_fd, True)
        except OSError as exc:
            self.error = f"no se pudo abrir el FIFO: {exc}"
            self._fifo_fd = None
        return self._fifo_fd

    def _loop(self) -> None:
        while not self._stop:
            stdin = subprocess.DEVNULL
            if self.mode == "pipe":
                fd = self._open_fifo()
                if fd is None:
                    return
                stdin = fd
            try:
                proc = subprocess.Popen(
                    self._args(), stdin=stdin,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, **_DETACHED)
            except OSError as exc:
                self.error = f"no se pudo lanzar ffmpeg: {exc}"
                return
            self._proc = proc
            self.error = None
            stream = proc.stdout
            try:
                while not self._stop and stream is not None:
                    chunk = stream.read(CHUNK)
                    if not chunk:
                        break
                    self.analysis.feed(chunk)
            except (OSError, ValueError):
                pass
            self.analysis.silence()
            if self._stop:
                break
            detail = b""
            try:
                if proc.stderr is not None:
                    detail = proc.stderr.read() or b""
            except (OSError, ValueError):
                pass
            if proc.poll() is None:
                try:
                    proc.kill()
                except OSError:
                    pass
            if self.mode == "loopback" and detail:
                self.error = detail.decode("utf-8", "replace").strip().splitlines()[-1][:120]
                return
            # En modo FIFO es normal: librespot cerró al acabar la pista.
            if self._stop:
                break


def build(lag: float, mode: str, source: str) -> tuple[StreamAnalysis, PcmPump]:
    analysis = StreamAnalysis(lag=lag)
    return analysis, PcmPump(analysis, mode, source)
