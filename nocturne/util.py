"""Utilidades compartidas: texto, tiempo y orden natural."""
from __future__ import annotations

import re
import unicodedata

AUDIO_EXTS = {
    ".mp3", ".flac", ".m4a", ".aac", ".wav", ".ogg", ".oga", ".opus",
    ".wma", ".aiff", ".aif", ".alac", ".ape", ".mp4", ".m4b", ".webm",
}

_ESC_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def char_width(ch: str) -> int:
    """Ancho en celdas de un carácter (aprox. suficiente para nombres de pista)."""
    if unicodedata.combining(ch):
        return 0
    if unicodedata.east_asian_width(ch) in ("W", "F"):
        return 2
    return 1


def text_width(s: str) -> int:
    return sum(char_width(c) for c in s)


def truncate(s: str, width: int, ellipsis: str = "…") -> str:
    """Recorta a `width` celdas añadiendo elipsis si hace falta."""
    if width <= 0:
        return ""
    if text_width(s) <= width:
        return s
    if width == 1:
        return ellipsis
    out, used = [], 0
    limit = width - 1
    for ch in s:
        w = char_width(ch)
        if used + w > limit:
            break
        out.append(ch)
        used += w
    return "".join(out) + ellipsis


def pad(s: str, width: int) -> str:
    """Recorta y rellena hasta ocupar exactamente `width` celdas."""
    s = truncate(s, width)
    return s + " " * max(0, width - text_width(s))


def fit(s: str, width: int, align: str = "left") -> str:
    s = truncate(s, width)
    gap = max(0, width - text_width(s))
    if align == "right":
        return " " * gap + s
    if align == "center":
        left = gap // 2
        return " " * left + s + " " * (gap - left)
    return s + " " * gap


def fmt_time(seconds: float | None) -> str:
    """Segundos -> m:ss (o h:mm:ss)."""
    if seconds is None or seconds < 0 or seconds != seconds:
        return "--:--"
    total = int(seconds)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


_NAT_RE = re.compile(r"(\d+)")


def natural_key(name: str):
    """Orden natural: 'pista2' antes que 'pista10'."""
    parts = _NAT_RE.split(name.lower())
    return [int(p) if p.isdigit() else p for p in parts]


def visible_len(ansi: str) -> int:
    """Ancho visible de una línea con secuencias ANSI."""
    return text_width(_ESC_RE.sub("", ansi))


def strip_ansi(s: str) -> str:
    return _ESC_RE.sub("", s)


def clamp(value, low, high):
    return max(low, min(high, value))


def lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t
