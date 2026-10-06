"""Carátulas: extracción con ffmpeg y render a ANSI con chafa."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import threading
import urllib.error
import urllib.request

from .util import strip_ansi, text_width

COVER_NAMES = ("cover", "folder", "front", "album", "artwork", "art", "thumb")
COVER_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif")
DOWNLOAD_TIMEOUT = 15.0
MAX_IMAGE_BYTES = 8 << 20
_SGR_ONLY = re.compile(r"\x1b\[(?![0-9;]*m)[0-9;?]*[A-Za-z]")

HAS_CHAFA = shutil.which("chafa") is not None
# Todos los procesos hijo van en su propia sesión: sin terminal de control no
# pueden abrir /dev/tty ni robar las teclas del usuario (chafa lo intenta).
_DETACHED = {"start_new_session": True}


def _chafa_supports_probe() -> bool:
    """chafa >= 1.16 permite desactivar el sondeo del terminal."""
    if not HAS_CHAFA:
        return False
    try:
        out = subprocess.run(["chafa", "--help"], stdin=subprocess.DEVNULL,
                             capture_output=True, text=True, timeout=10, **_DETACHED)
        return "--probe" in (out.stdout + out.stderr)
    except Exception:
        return False


_PROBE_FLAG: bool | None = None


def _sanitize(line: str) -> str:
    """Deja sólo secuencias de color; nada que mueva el cursor."""
    return _SGR_ONLY.sub("", line)


def find_cover_file(track_path: str) -> str | None:
    """Busca una imagen de carátula junto al archivo."""
    folder = os.path.dirname(track_path)
    try:
        names = os.listdir(folder)
    except OSError:
        return None
    lowered = {n.lower(): n for n in names}
    for base in COVER_NAMES:
        for ext in COVER_EXTS:
            got = lowered.get(base + ext)
            if got:
                return os.path.join(folder, got)
    # Cualquier imagen que comparta nombre con la pista.
    stem = os.path.splitext(os.path.basename(track_path))[0].lower()
    for ext in COVER_EXTS:
        got = lowered.get(stem + ext)
        if got:
            return os.path.join(folder, got)
    return None


def extract_embedded(track_path: str, dest_dir: str) -> str | None:
    """Vuelca la carátula incrustada a PNG. None si la pista no tiene."""
    dest = os.path.join(dest_dir, "cover.png")
    try:
        proc = subprocess.run(
            ["ffmpeg", "-v", "quiet", "-y", "-i", track_path,
             "-an", "-map", "0:v:0", "-frames:v", "1",
             "-vf", "scale='min(640,iw)':-2", "-c:v", "png", dest],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=25, **_DETACHED)
    except Exception:
        return None
    if proc.returncode == 0 and os.path.exists(dest) and os.path.getsize(dest) > 256:
        return dest
    return None


def download(url: str, dest_dir: str) -> str | None:
    """Baja una carátula remota a disco. None si no se pudo."""
    if not url.startswith(("http://", "https://")):
        return None
    dest = os.path.join(dest_dir, "remote.img")
    try:
        os.makedirs(dest_dir, exist_ok=True)
        req = urllib.request.Request(url, headers={"User-Agent": "nocturne/1.0"})
        with urllib.request.urlopen(req, timeout=DOWNLOAD_TIMEOUT) as resp:
            data = resp.read(MAX_IMAGE_BYTES + 1)
    except (urllib.error.URLError, OSError, ValueError):
        return None
    if not data or len(data) > MAX_IMAGE_BYTES:
        return None
    try:
        with open(dest, "wb") as fh:
            fh.write(data)
    except OSError:
        return None
    return dest


def render(image_path: str, width: int, height: int, symbols: str) -> list[str]:
    """Convierte una imagen en `height` líneas ANSI de `width` celdas."""
    global _PROBE_FLAG
    if not HAS_CHAFA or width < 2 or height < 2:
        return []
    if _PROBE_FLAG is None:
        _PROBE_FLAG = _chafa_supports_probe()
    cmd = ["chafa", "--format=symbols", "--colors=full", "--polite=on",
           "--animate=off", f"--size={width}x{height}"]
    if _PROBE_FLAG:
        # Sin esto chafa consulta al terminal y espera hasta 5 s la respuesta.
        cmd.append("--probe=off")
    if symbols:
        cmd.append(f"--symbols={symbols}")
    cmd.append(image_path)
    try:
        out = subprocess.run(cmd, stdin=subprocess.DEVNULL,
                             capture_output=True, text=True, timeout=25, **_DETACHED)
    except Exception:
        return []
    if out.returncode != 0:
        return []
    lines = [_sanitize(l) for l in out.stdout.split("\n") if strip_ansi(l).strip()]
    return lines[:height]


class ArtworkCache:
    """Resuelve carátulas en segundo plano; el último pedido tiene prioridad."""

    def __init__(self, symbols: str = "vhalf+block+space") -> None:
        self.symbols = symbols
        self.enabled = HAS_CHAFA
        self._art: dict[tuple[str, int, int], list[str]] = {}
        self._images: dict[str, str | None] = {}
        self._urls: dict[str, str] = {}
        self._pending: tuple[str, int, int] | None = None
        self._inflight: tuple[str, int, int] | None = None
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = False
        self._tmpdir = tempfile.mkdtemp(prefix="nocturne-art-")
        self._worker = threading.Thread(target=self._loop, daemon=True)
        self._worker.start()

    def get(self, track_path: str, width: int, height: int) -> list[str] | None:
        """Líneas ANSI si están listas; si no, encola el trabajo y devuelve None."""
        if not self.enabled or not track_path or width < 2 or height < 2:
            return None
        key = (track_path, width, height)
        with self._lock:
            got = self._art.get(key)
            if got is not None:
                return got or None
            if self._pending != key and self._inflight != key:
                self._pending = key
                self._wake.set()
        return None

    def has_failed(self, track_path: str, width: int, height: int) -> bool:
        with self._lock:
            return self._art.get((track_path, width, height)) == []

    def set_url(self, track_path: str, url: str) -> None:
        """Apunta la carátula remota de una pista (Spotify) antes de pedirla."""
        if not url:
            return
        with self._lock:
            if self._urls.get(track_path) == url:
                return
            self._urls[track_path] = url
            self._images.pop(track_path, None)

    def image_for(self, track_path: str) -> str | None:
        with self._lock:
            return self._images.get(track_path)

    def close(self) -> None:
        self._stop = True
        self._wake.set()
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _loop(self) -> None:
        while not self._stop:
            self._wake.wait()
            self._wake.clear()
            while not self._stop:
                with self._lock:
                    key, self._pending = self._pending, None
                    self._inflight = key
                if key is None:
                    break
                track, width, height = key
                lines = self._resolve(track, width, height)
                with self._lock:
                    self._art[key] = lines
                    self._inflight = None

    def _resolve(self, track: str, width: int, height: int) -> list[str]:
        with self._lock:
            known = track in self._images
            image = self._images.get(track)
        if not known:
            with self._lock:
                url = self._urls.get(track, "")
            folder = os.path.join(self._tmpdir, f"{abs(hash(track)):x}")
            if url:
                image = download(url, folder)
            else:
                image = find_cover_file(track)
                if image is None:
                    os.makedirs(folder, exist_ok=True)
                    image = extract_embedded(track, folder)
            with self._lock:
                self._images[track] = image
        if not image:
            return []
        return render(image, width, height, self.symbols)


def pad_lines(lines: list[str], width: int) -> list[str]:
    """Centra horizontalmente las líneas si chafa devolvió menos ancho."""
    out = []
    for line in lines:
        visible = text_width(strip_ansi(line))
        gap = max(0, width - visible)
        left = gap // 2
        out.append(" " * left + line + "\x1b[0m" + " " * (gap - left))
    return out
