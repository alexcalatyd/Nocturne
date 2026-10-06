"""librespot como altavoz propio de Nocturne.

Si está instalado y ya tiene credenciales guardadas, Nocturne lo arranca y lo
mata con la sesión: así no hay que tener dos ventanas abiertas. La primera vez
hay que autorizarlo con `nocturne --spotify-device-login`, que es un OAuth
aparte del de Nocturne (librespot usa su propio client id de Spotify).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time

CACHE_DIR = os.path.join(
    os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"),
    "nocturne", "librespot")
CREDENTIALS = os.path.join(CACHE_DIR, "credentials.json")
AUDIO_CACHE = os.path.join(CACHE_DIR, "audio")
AUDIO_CACHE_LIMIT = "1G"
DEVICE_NAME = "Nocturne"
STARTUP_GRACE = 6.0          # s que le damos para aparecer en Connect


def installed() -> bool:
    return shutil.which("librespot") is not None


def authorized() -> bool:
    return os.path.exists(CREDENTIALS)


def _base_args(fifo: str, name: str, bitrate: int, volume: int) -> list[str]:
    # `--system-cache` sólo guarda credenciales y volumen. Sin `--cache`,
    # librespot vuelve a bajarse del CDN hasta la pista que acabas de oír, y eso
    # se nota en cada cambio de canción.
    return [
        "librespot",
        "--name", name,
        "--bitrate", str(bitrate),
        "--backend", "pipe",
        "--device", fifo,
        "--initial-volume", str(max(0, min(100, volume))),
        "--system-cache", CACHE_DIR,
        "--cache", AUDIO_CACHE,
        "--cache-size-limit", AUDIO_CACHE_LIMIT,
    ]


def login(fifo: str, name: str = DEVICE_NAME, bitrate: int = 320,
          volume: int = 70) -> int:
    """OAuth interactivo de librespot. Devuelve el código de salida."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    args = _base_args(fifo, name, bitrate, volume) + ["--enable-oauth"]
    print("Autorizando el altavoz de Nocturne en Spotify.")
    print("Se abrirá (o se imprimirá) un enlace: acéptalo y vuelve aquí.\n")
    try:
        proc = subprocess.Popen(args)
    except OSError as exc:
        print(f"No se pudo lanzar librespot: {exc}")
        return 1
    deadline = time.monotonic() + 300.0
    try:
        while time.monotonic() < deadline:
            if authorized():
                time.sleep(1.0)          # deja que acabe de escribir el fichero
                print("\nListo: el altavoz ya está autorizado.")
                return 0
            if proc.poll() is not None:
                break
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
    if authorized():
        print("\nListo: el altavoz ya está autorizado.")
        return 0
    print("\nNo llegó la autorización.")
    return 1


class Librespot:
    """Proceso hijo de librespot, vivo mientras dure Nocturne."""

    def __init__(self, fifo: str, name: str = DEVICE_NAME, bitrate: int = 320,
                 volume: int = 70) -> None:
        self.fifo = fifo
        self.name = name
        self.bitrate = bitrate
        self.volume = volume
        self.error: str | None = None
        self._proc: subprocess.Popen | None = None

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start(self) -> bool:
        if self.alive:
            return True
        if not installed():
            self.error = "librespot no está instalado"
            return False
        if not authorized():
            self.error = "el altavoz no está autorizado (nocturne --spotify-device-login)"
            return False
        # NOCTURNE_LIBRESPOT_LOG=fichero guarda la salida de librespot, que es
        # lo único que explica por qué una pista no suena.
        log_path = os.environ.get("NOCTURNE_LIBRESPOT_LOG")
        log = None
        try:
            if log_path:
                log = open(log_path, "ab", buffering=0)
            self._proc = subprocess.Popen(
                _base_args(self.fifo, self.name, self.bitrate, self.volume),
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=log or subprocess.DEVNULL, start_new_session=True)
        except OSError as exc:
            self.error = f"no se pudo lanzar librespot: {exc}"
            return False
        finally:
            if log is not None:
                log.close()
        self.error = None
        return True

    def stop(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None or proc.poll() is not None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=3.0)
        except (OSError, subprocess.TimeoutExpired):
            try:
                proc.kill()
            except OSError:
                pass
