"""Integración con Spotify: navegación, control de reproducción y PCM."""
from __future__ import annotations

SCHEME = "spotify:"


def is_spotify(path: str) -> bool:
    return path.startswith(SCHEME)
