"""Exploración de la biblioteca (carpetas locales y Spotify) y metadatos."""
from __future__ import annotations

import json
import os
import subprocess
import threading
from dataclasses import dataclass, field

from .util import AUDIO_EXTS, natural_key

PARENT = ".."


@dataclass
class Entry:
    path: str
    name: str
    is_dir: bool
    is_parent: bool = False

    @property
    def ext(self) -> str:
        return os.path.splitext(self.name)[1].lower()


@dataclass
class Meta:
    """Metadatos de una pista; los campos vacíos se rellenan con el archivo."""
    path: str
    title: str = ""
    artist: str = ""
    album: str = ""
    date: str = ""
    genre: str = ""
    track: str = ""
    duration: float = 0.0
    codec: str = ""
    bitrate: int = 0
    sample_rate: int = 0
    channels: int = 0
    has_cover: bool = False
    art_url: str = ""        # carátula remota (Spotify)
    loaded: bool = False
    failed: bool = False
    extra: dict = field(default_factory=dict)

    @property
    def display_title(self) -> str:
        if self.title:
            return self.title
        if self.path.startswith("spotify:"):
            return "Pista de Spotify"
        return os.path.splitext(os.path.basename(self.path))[0]

    @property
    def display_artist(self) -> str:
        return self.artist or "Artista desconocido"

    @property
    def is_remote(self) -> bool:
        return self.path.startswith("spotify:")

    @property
    def quality(self) -> str:
        if self.is_remote:
            bits = ["Spotify"]
            if self.date:
                bits.append(self.date[:4])
            return " · ".join(b for b in bits if b)
        bits = []
        if self.codec:
            bits.append(self.codec.upper())
        if self.bitrate:
            bits.append(f"{self.bitrate // 1000} kbps")
        if self.sample_rate:
            bits.append(f"{self.sample_rate / 1000:.1f} kHz".replace(".0 ", " "))
        if self.channels == 1:
            bits.append("mono")
        elif self.channels == 2:
            bits.append("estéreo")
        return " · ".join(bits)


def _probe(path: str) -> Meta:
    meta = Meta(path=path)
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_format", "-show_streams", path],
            stdin=subprocess.DEVNULL, capture_output=True, timeout=20,
            start_new_session=True)
        if out.returncode != 0:
            meta.failed = True
            return meta
        data = json.loads(out.stdout or b"{}")
    except Exception:
        meta.failed = True
        return meta

    fmt = data.get("format", {})
    try:
        meta.duration = float(fmt.get("duration") or 0.0)
    except (TypeError, ValueError):
        meta.duration = 0.0
    try:
        meta.bitrate = int(fmt.get("bit_rate") or 0)
    except (TypeError, ValueError):
        meta.bitrate = 0

    stream_bitrate = 0
    tags = {str(k).lower(): str(v) for k, v in (fmt.get("tags") or {}).items()}
    for stream in data.get("streams", []):
        if stream.get("codec_type") == "audio":
            meta.codec = stream.get("codec_name", "") or meta.codec
            try:
                meta.sample_rate = int(stream.get("sample_rate") or 0)
            except (TypeError, ValueError):
                pass
            meta.channels = stream.get("channels") or 0
            try:
                stream_bitrate = int(stream.get("bit_rate") or 0)
            except (TypeError, ValueError):
                stream_bitrate = 0
            for k, v in (stream.get("tags") or {}).items():
                tags.setdefault(str(k).lower(), str(v))
        elif stream.get("codec_type") == "video":
            if stream.get("disposition", {}).get("attached_pic"):
                meta.has_cover = True

    # El del contenedor incluye la carátula incrustada e infla la cifra.
    if stream_bitrate:
        meta.bitrate = stream_bitrate

    meta.title = tags.get("title", "")
    meta.artist = tags.get("artist") or tags.get("album_artist", "")
    meta.album = tags.get("album", "")
    meta.date = (tags.get("date") or tags.get("year", ""))[:10]
    meta.genre = tags.get("genre", "")
    meta.track = tags.get("track", "").split("/")[0]
    meta.extra = tags
    meta.loaded = True
    return meta


class MetadataCache:
    """Sondea pistas con ffprobe en segundo plano, lo visible primero."""

    def __init__(self, workers: int = 3) -> None:
        self._cache: dict[str, Meta] = {}
        self._stack: list[str] = []          # LIFO: lo último pedido es lo visible
        self._queued: set[str] = set()
        self._lock = threading.Lock()
        self._wake = threading.Semaphore(0)
        self._stop = False
        self._threads = [threading.Thread(target=self._worker, daemon=True)
                         for _ in range(workers)]
        for t in self._threads:
            t.start()

    def put(self, meta: Meta) -> None:
        """Inyecta metadatos que ya vienen resueltos (p. ej. del API de Spotify)."""
        with self._lock:
            self._cache[meta.path] = meta
            self._queued.discard(meta.path)

    def get(self, path: str, request: bool = True) -> Meta | None:
        """Metadatos ya conocidos, o None encolando el sondeo."""
        with self._lock:
            got = self._cache.get(path)
            if got is not None or not request:
                return got
            if path.startswith("spotify:"):
                return None          # los sirve el listado, no ffprobe
            if path not in self._queued:
                self._queued.add(path)
                self._stack.append(path)
                self._wake.release()
        return None

    def get_blocking(self, path: str, timeout: float = 20.0) -> Meta:
        with self._lock:
            got = self._cache.get(path)
        if got is not None:
            return got
        if path.startswith("spotify:"):
            # Sin listado previo no hay nada que sondear localmente.
            return Meta(path=path, failed=True)
        meta = _probe(path)
        with self._lock:
            self._cache[path] = meta
        return meta

    def peek(self, path: str) -> Meta | None:
        with self._lock:
            return self._cache.get(path)

    def close(self) -> None:
        self._stop = True
        for _ in self._threads:
            self._wake.release()

    def _worker(self) -> None:
        while not self._stop:
            self._wake.acquire()
            if self._stop:
                return
            with self._lock:
                if not self._stack:
                    continue
                path = self._stack.pop()
            meta = _probe(path)
            with self._lock:
                self._cache[path] = meta
                self._queued.discard(path)


class Library:
    """Listado navegable: el árbol local de carpetas o el de Spotify."""

    def __init__(self, directory: str, spotify=None) -> None:
        self.location = os.path.abspath(directory)
        self.spotify = spotify
        self.entries: list[Entry] = []
        self.error: str | None = None
        self.loading = False
        self.show_all = False        # incluir archivos no reconocidos como audio
        self._resume = {"local": self.location, "spotify": "spotify:root"}
        self.refresh()

    # -- ubicación ---------------------------------------------------------
    @property
    def on_spotify(self) -> bool:
        return self.location.startswith("spotify:")

    @property
    def directory(self) -> str:
        """Carpeta local actual; en modo Spotify, la última visitada."""
        return self._resume["local"]

    @property
    def title(self) -> str:
        if self.on_spotify:
            return self.spotify.title(self.location) if self.spotify else "Spotify"
        home = os.path.expanduser("~")
        path = self.location
        return "~" + path[len(home):] if path.startswith(home) else path

    def toggle_source(self) -> bool:
        """Salta entre el árbol local y el de Spotify. False si no hay Spotify."""
        if not self.on_spotify and self.spotify is None:
            return False
        target = self._resume["local" if self.on_spotify else "spotify"]
        self._remember()
        self.location = target
        self.refresh()
        return True

    def _remember(self) -> None:
        self._resume["spotify" if self.on_spotify else "local"] = self.location

    # -- listados ----------------------------------------------------------
    def refresh(self) -> None:
        if self.on_spotify:
            if self.spotify is not None:
                self.spotify.invalidate(self.location)
            self._refresh_spotify()
        else:
            self._refresh_local()

    def poll(self) -> bool:
        """Recoge un listado remoto que acabe de llegar. True si ya está."""
        if not (self.on_spotify and self.loading):
            return False
        self._refresh_spotify()
        return not self.loading

    def _refresh_spotify(self) -> None:
        if self.spotify is None:
            self.entries, self.error, self.loading = [], "Spotify no está conectado", False
            return
        got = self.spotify.peek(self.location)
        if got is None:
            # Vaciar mientras llega: si no, se queda a la vista el listado
            # anterior y el cursor apunta a una fila que ya no existe.
            self.loading = True
            self.error = None
            if self.entries:
                self.entries = []
            return
        items, error = got
        self.loading = False
        self.error = error
        entries: list[Entry] = []
        parent = self.spotify.parent(self.location)
        if parent:
            entries.append(Entry(parent, PARENT, True, is_parent=True))
        entries.extend(items)
        self.entries = entries

    def _refresh_local(self) -> None:
        dirs: list[Entry] = []
        files: list[Entry] = []
        self.error = None
        self.loading = False
        try:
            with os.scandir(self.location) as it:
                for item in it:
                    if item.name.startswith("."):
                        continue
                    try:
                        is_dir = item.is_dir(follow_symlinks=True)
                    except OSError:
                        continue
                    if is_dir:
                        dirs.append(Entry(item.path, item.name, True))
                    else:
                        ext = os.path.splitext(item.name)[1].lower()
                        if self.show_all or ext in AUDIO_EXTS:
                            files.append(Entry(item.path, item.name, False))
        except PermissionError:
            self.error = "Sin permiso para leer esta carpeta"
        except OSError as exc:
            self.error = f"No se pudo leer la carpeta: {exc.strerror or exc}"

        dirs.sort(key=lambda e: natural_key(e.name))
        files.sort(key=lambda e: natural_key(e.name))

        entries: list[Entry] = []
        parent = os.path.dirname(self.location)
        if parent and parent != self.location:
            entries.append(Entry(parent, PARENT, True, is_parent=True))
        entries.extend(dirs)
        entries.extend(files)
        self.entries = entries

    # -- navegación --------------------------------------------------------
    def cd(self, path: str) -> bool:
        if path.startswith("spotify:"):
            if self.spotify is None:
                return False
            self._remember()
            self.location = path
            self._refresh_spotify()
            return True
        target = os.path.abspath(path)
        if not os.path.isdir(target):
            return False
        self._remember()
        self.location = target
        self._refresh_local()
        return True

    def up(self) -> bool:
        if self.on_spotify:
            parent = self.spotify.parent(self.location) if self.spotify else None
            return self.cd(parent) if parent else False
        parent = os.path.dirname(self.location)
        if not parent or parent == self.location:
            return False
        return self.cd(parent)

    @property
    def tracks(self) -> list[Entry]:
        return [e for e in self.entries if not e.is_dir]

    def index_of(self, path: str) -> int | None:
        for i, entry in enumerate(self.entries):
            if entry.path == path:
                return i
        return None
