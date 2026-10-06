"""Monta (o no) toda la parte de Spotify y se la da hecha a la aplicación."""
from __future__ import annotations

import os

from . import auth, capture, device
from .client import ApiError, Client
from .player import SpotifyPlayer
from .source import ROOT, SpotifySource

DEFAULT_FIFO = os.path.join(
    os.environ.get("TMPDIR", "/tmp").rstrip("/"), "nocturne-librespot.pcm")


class SpotifySession:
    """Cliente, árbol de navegación, reproductor y captura de PCM."""

    def __init__(self, volume: int = 80, audio: str = "auto",
                 fifo: str = "", lag: float = 0.22, on_meta=None) -> None:
        self.volume = volume
        self.error: str | None = None
        self.client: Client | None = None
        self.source: SpotifySource | None = None
        self.player: SpotifyPlayer | None = None
        self.analysis = None
        self.pump: capture.PcmPump | None = None
        self.audio_mode = "none"
        self.audio_detail = ""
        self.librespot: device.Librespot | None = None

        if not auth.has_credentials():
            self.error = "Spotify no está conectado (usa: nocturne --spotify-login)"
            return
        try:
            self.client = Client()
            self.source = SpotifySource(self.client, on_meta=on_meta)
            self.player = SpotifyPlayer(self.client, volume=volume)
            self.player.preferred_device = device.DEVICE_NAME
        except Exception as exc:
            self.error = f"no se pudo preparar Spotify: {exc}"
            self.client = self.source = self.player = None
            return
        self._setup_audio(audio, fifo or DEFAULT_FIFO, lag)

    @property
    def available(self) -> bool:
        return self.source is not None and self.player is not None

    @property
    def root(self) -> str:
        return ROOT

    # -- espectro ----------------------------------------------------------
    def _setup_audio(self, mode: str, fifo: str, lag: float) -> None:
        if mode == "none":
            self.audio_detail = "espectro desactivado"
            return
        if mode in ("auto", "pipe"):
            if mode == "pipe" or device.installed() or os.path.exists(fifo):
                try:
                    capture.make_fifo(fifo)
                except OSError as exc:
                    if mode == "pipe":
                        self.audio_detail = f"no se pudo crear el FIFO: {exc}"
                        return
                else:
                    self.analysis, self.pump = capture.build(lag, "pipe", fifo)
                    self.audio_mode = "pipe"
                    if device.installed() and device.authorized():
                        self.librespot = device.Librespot(fifo, volume=self.volume)
                        self.audio_detail = "librespot (lo arranca Nocturne)"
                        if self.player is not None:
                            self.player.expect_preferred = True
                    elif device.installed():
                        self.audio_detail = ("librespot sin autorizar: "
                                             "nocturne --spotify-device-login")
                    else:
                        self.audio_detail = fifo
                    return
        if mode in ("auto", "loopback"):
            found = capture.find_loopback()
            if found is not None:
                index, name = found
                self.analysis, self.pump = capture.build(lag, "loopback", str(index))
                self.audio_mode = "loopback"
                self.audio_detail = name
                return
            if mode == "loopback":
                self.audio_detail = "no encontré ningún dispositivo de bucle"
                return
        self.audio_detail = "sin fuente de PCM: Spotify sonará sin espectro"

    def start_audio(self) -> None:
        """Arranca el altavoz propio (si lo hay) y el bombeo de PCM."""
        if self.librespot is not None and not self.librespot.alive:
            if not self.librespot.start() and self.librespot.error:
                self.audio_detail = self.librespot.error
        if self.pump is not None and not self.pump.alive:
            self.pump.start()

    def stop_audio(self) -> None:
        if self.pump is not None:
            self.pump.stop()

    # -- utilidades --------------------------------------------------------
    def devices(self) -> list[dict]:
        if self.client is None:
            return []
        try:
            return self.client.devices()
        except ApiError:
            return []

    def close(self) -> None:
        self.stop_audio()
        if self.librespot is not None:
            self.librespot.stop()
        if self.player is not None:
            self.player.close()
        if self.analysis is not None:
            self.analysis.close()
        if self.client is not None:
            self.client.close()
