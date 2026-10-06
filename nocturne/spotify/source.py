"""El árbol de Spotify como si fuera un sistema de carpetas.

Las URIs virtuales («spotify:playlist:abc») hacen de ruta, así el resto de
Nocturne sigue tratando todo como `Entry.path`. Cada listado se trae en
segundo plano: la interfaz nunca se bloquea esperando a la red.
"""
from __future__ import annotations

import threading
import time
import urllib.parse

from ..library import Entry, Meta
from .client import ApiError, Client

ROOT = "spotify:root"
PLAYLISTS = "spotify:playlists"
SAVED = "spotify:saved"
ALBUMS = "spotify:albums"
ARTISTS = "spotify:artists"
SEARCH_PREFIX = "spotify:search:"

CACHE_TTL = 120.0            # s antes de volver a preguntar por un listado
TRACK_CAP = 2000
SEARCH_PAGE = 10             # el máximo que acepta hoy /search
SEARCH_PAGES = 4             # hasta 40 resultados, de sobra para elegir

_ROOT_MENU = (
    (PLAYLISTS, "Playlists"),
    (SAVED, "Canciones guardadas"),
    (ALBUMS, "Álbumes guardados"),
    (ARTISTS, "Artistas seguidos"),
)
_SECTION_TITLES = dict(_ROOT_MENU)


def search_location(query: str) -> str:
    return SEARCH_PREFIX + urllib.parse.quote(query, safe="")


def _ms(value) -> float:
    try:
        return max(0.0, float(value) / 1000.0)
    except (TypeError, ValueError):
        return 0.0


def _unwrap(wrapper: dict | None) -> dict:
    """La pista dentro de un elemento de playlist o de «guardadas»."""
    wrapper = wrapper or {}
    return wrapper.get("item") or wrapper.get("track") or {}


def _artists(node: dict) -> str:
    names = [a.get("name", "") for a in (node.get("artists") or []) if a.get("name")]
    return ", ".join(names)


def _image(node: dict) -> str:
    images = node.get("images") or []
    if not images:
        return ""
    # La mediana basta: chafa la va a reducir a un puñado de celdas.
    ordered = sorted(images, key=lambda i: i.get("width") or 0, reverse=True)
    pick = ordered[len(ordered) // 2] if len(ordered) > 2 else ordered[0]
    return pick.get("url", "")


def track_meta(track: dict, art_url: str = "") -> Meta | None:
    """Convierte un objeto «track» del API en el Meta de Nocturne."""
    uri = track.get("uri") or ""
    if not uri.startswith("spotify:track:"):
        return None
    album = track.get("album") or {}
    meta = Meta(path=uri)
    meta.title = track.get("name", "")
    meta.artist = _artists(track)
    meta.album = album.get("name", "")
    meta.date = (album.get("release_date") or "")[:10]
    meta.duration = _ms(track.get("duration_ms"))
    meta.track = str(track.get("track_number") or "")
    meta.codec = "spotify"
    meta.art_url = art_url or _image(album)
    meta.has_cover = bool(meta.art_url)
    meta.loaded = True
    meta.extra = {"explicit": "1" if track.get("explicit") else "",
                  "popularity": str(track.get("popularity") or "")}
    return meta


class SpotifySource:
    """Listados del árbol de Spotify, cacheados y resueltos en segundo plano."""

    def __init__(self, client: Client, on_meta=None) -> None:
        self.client = client
        self._on_meta = on_meta
        self._cache: dict[str, tuple[float, list[Entry], str | None]] = {}
        self._parents: dict[str, str] = {}
        self._titles: dict[str, str] = {}
        self._contexts: dict[str, str] = {}   # location -> context_uri reproducible
        self._user_id = ""
        self._inflight: set[str] = set()
        self._lock = threading.Lock()

    # -- API para Library --------------------------------------------------
    def peek(self, location: str) -> tuple[list[Entry], str | None] | None:
        """Listado ya resuelto, o None si hay que esperar (lanza la carga)."""
        with self._lock:
            got = self._cache.get(location)
            if got and time.monotonic() - got[0] < CACHE_TTL:
                return got[1], got[2]
            if location in self._inflight:
                return None
            self._inflight.add(location)
        threading.Thread(target=self._fetch, args=(location,), daemon=True).start()
        return None

    def invalidate(self, location: str | None = None) -> None:
        with self._lock:
            if location is None:
                self._cache.clear()
            else:
                self._cache.pop(location, None)

    def title(self, location: str) -> str:
        if location == ROOT:
            return "Spotify"
        fixed = _SECTION_TITLES.get(location)
        if fixed:
            return f"Spotify / {fixed}"
        with self._lock:
            known = self._titles.get(location)
        if known:
            return f"Spotify / {known}"
        if location.startswith(SEARCH_PREFIX):
            return f"Spotify / «{urllib.parse.unquote(location[len(SEARCH_PREFIX):])}»"
        return "Spotify"

    def parent(self, location: str) -> str | None:
        if location == ROOT:
            return None
        with self._lock:
            known = self._parents.get(location)
        if known:
            return known
        if location in (PLAYLISTS, SAVED, ALBUMS, ARTISTS):
            return ROOT
        if location.startswith(SEARCH_PREFIX):
            return ROOT
        if location.startswith("spotify:playlist:"):
            return PLAYLISTS
        if location.startswith("spotify:album:"):
            return ALBUMS
        if location.startswith("spotify:artist:"):
            return ARTISTS
        return ROOT

    def context_for(self, location: str) -> str | None:
        """Contexto reproducible del listado actual.

        Spotify (y sobre todo librespot) reproduce con garantías dentro de un
        contexto —playlist, álbum, «tus me gusta»—; una lista suelta de URIs
        se corta a los pocos segundos. Los resultados de búsqueda no tienen
        contexto propio: ahí se cae a la lista y se acepta el riesgo.
        """
        if location == SAVED:
            return f"spotify:user:{self._me()}:collection" if self._me() else None
        with self._lock:
            return self._contexts.get(location)

    def _me(self) -> str:
        with self._lock:
            if self._user_id:
                return self._user_id
        try:
            user_id = (self.client.me() or {}).get("id", "")
        except Exception:
            return ""
        with self._lock:
            self._user_id = user_id
        return user_id

    # -- resolución --------------------------------------------------------
    def _fetch(self, location: str) -> None:
        try:
            entries, error = self._build(location)
        except ApiError as exc:
            entries, error = [], str(exc)
        except Exception as exc:                      # red, JSON raro, etc.
            entries, error = [], f"Spotify: {exc}"
        with self._lock:
            self._cache[location] = (time.monotonic(), entries, error)
            self._inflight.discard(location)

    def _build(self, location: str) -> tuple[list[Entry], str | None]:
        if location == ROOT:
            return [Entry(uri, name, True) for uri, name in _ROOT_MENU], None
        if location == PLAYLISTS:
            return self._playlists(), None
        if location == SAVED:
            return self._saved(), None
        if location == ALBUMS:
            return self._albums(), None
        if location == ARTISTS:
            return self._artists_followed(), None
        if location.startswith(SEARCH_PREFIX):
            return self._search(urllib.parse.unquote(location[len(SEARCH_PREFIX):])), None
        if location.startswith("spotify:playlist:"):
            return self._playlist_tracks(location), None
        if location.startswith("spotify:album:"):
            return self._album_tracks(location), None
        if location.startswith("spotify:artist:"):
            return self._artist_albums(location), None
        return [], "Sección de Spotify desconocida"

    def _remember(self, child: str, parent: str, title: str,
                  context: str | None = None) -> None:
        with self._lock:
            self._parents[child] = parent
            self._titles[child] = title
            if context:
                self._contexts[child] = context

    def _publish(self, meta: Meta | None) -> None:
        if meta is not None and self._on_meta is not None:
            self._on_meta(meta)

    def _playlists(self) -> list[Entry]:
        out = []
        for item in self.client.pages("/me/playlists", limit=50, cap=500):
            uri = item.get("uri") or ""
            if not uri:
                continue
            block = item.get("items") or item.get("tracks") or {}
            total = block.get("total") or 0
            name = item.get("name") or "(sin nombre)"
            self._remember(uri, PLAYLISTS, name, context=uri)
            with self._lock:
                self._titles[uri] = name
            out.append(Entry(uri, f"{name}  ·  {total}", True))
        return out

    def _saved(self) -> list[Entry]:
        out = []
        for item in self.client.pages("/me/tracks", limit=50, cap=TRACK_CAP):
            out.append(self._track_entry(_unwrap(item)))
        return [e for e in out if e is not None]

    def _albums(self) -> list[Entry]:
        out = []
        for item in self.client.pages("/me/albums", limit=50, cap=500):
            album = (item or {}).get("album") or {}
            uri = album.get("uri") or ""
            if not uri:
                continue
            name = album.get("name") or "(sin nombre)"
            label = f"{_artists(album)} — {name}" if _artists(album) else name
            self._remember(uri, ALBUMS, label, context=uri)
            out.append(Entry(uri, label, True))
        return out

    def _artists_followed(self) -> list[Entry]:
        out = []
        page = self.client.get("/me/following", type="artist", limit=50)
        while page:
            block = page.get("artists") or {}
            for artist in block.get("items") or []:
                uri = artist.get("uri") or ""
                if not uri:
                    continue
                name = artist.get("name") or "(sin nombre)"
                self._remember(uri, ARTISTS, name)
                out.append(Entry(uri, name, True))
            nxt = block.get("next")
            if not nxt:
                break
            page = self.client.get(nxt)
        return out

    def _artist_albums(self, uri: str) -> list[Entry]:
        artist_id = uri.rsplit(":", 1)[-1]
        out = []
        for album in self.client.pages(f"/artists/{artist_id}/albums", limit=50,
                                       cap=200, include_groups="album,single"):
            album_uri = album.get("uri") or ""
            if not album_uri:
                continue
            year = (album.get("release_date") or "")[:4]
            name = album.get("name") or "(sin nombre)"
            label = f"{name}  ·  {year}" if year else name
            self._remember(album_uri, uri, name, context=album_uri)
            out.append(Entry(album_uri, label, True))
        return out

    def _playlist_tracks(self, uri: str) -> list[Entry]:
        playlist_id = uri.rsplit(":", 1)[-1]
        out = []
        for wrapper in self.client.pages(
                f"/playlists/{playlist_id}/items", limit=100, cap=TRACK_CAP,
                additional_types="track"):
            entry = self._track_entry(_unwrap(wrapper))
            if entry is not None:
                out.append(entry)
        return out

    def _album_tracks(self, uri: str) -> list[Entry]:
        album_id = uri.rsplit(":", 1)[-1]
        album = self.client.get(f"/albums/{album_id}")
        art = _image(album)
        shell = {"name": album.get("name"), "release_date": album.get("release_date"),
                 "images": album.get("images"), "uri": album.get("uri")}
        out = []
        for track in self.client.pages(f"/albums/{album_id}/tracks", limit=50,
                                       cap=TRACK_CAP):
            track = dict(track or {})
            track.setdefault("album", shell)
            entry = self._track_entry(track, art)
            if entry is not None:
                out.append(entry)
        return out

    def _search(self, query: str) -> list[Entry]:
        if not query.strip():
            return []
        out = []
        for page in range(SEARCH_PAGES):
            found = self.client.get("/search", q=query, type="track",
                                    limit=SEARCH_PAGE, offset=page * SEARCH_PAGE)
            items = (found.get("tracks") or {}).get("items") or []
            for track in items:
                entry = self._track_entry(track or {})
                if entry is not None:
                    out.append(entry)
            if len(items) < SEARCH_PAGE:
                break
        return out

    def _track_entry(self, track: dict, art_url: str = "") -> Entry | None:
        meta = track_meta(track, art_url)
        if meta is None:
            return None
        self._publish(meta)
        label = f"{meta.display_artist} — {meta.display_title}"
        return Entry(meta.path, label, False)
