"""Nocturne: reproductor de música para la terminal."""
from __future__ import annotations

import argparse
import curses
import os
import random
import shutil
import sys
import time
import unicodedata

from . import theme as T
from . import ui
from .analysis import TrackAnalysis
from .artwork import ArtworkCache
from .library import Library, Meta, MetadataCache
from .player import PAUSED, PLAYING, STOPPED, Player
from .spotify import is_spotify
from .spotify import source as spotify_source
from .spotify.session import SpotifySession
from .visualizer import MODES, Visualizer

REPEAT_MODES = ("off", "all", "one")
TOAST_SECONDS = 3.5
ART_REPAINT_EVERY = 2.0
SEEK_SMALL, SEEK_BIG = 5.0, 30.0


def _log(message: str) -> None:
    """Traza a NOCTURNE_DEBUG_LOG. Sin esa variable no hace nada."""
    path = os.environ.get("NOCTURNE_DEBUG_LOG")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%H:%M:%S')} {message}\n")
    except OSError:
        pass


def _fold(text: str) -> str:
    """Minúsculas sin acentos, para buscar sin pelearse con la tilde."""
    norm = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in norm if not unicodedata.combining(c))


class App:
    def __init__(self, stdscr, opts) -> None:
        self.scr = stdscr
        self.opts = opts
        self.theme = T.Theme()
        self.theme.setup()
        self.renderer = ui.Renderer(stdscr, self.theme)
        self.viz = Visualizer(self.theme, opts.viz)

        self.meta = MetadataCache()
        self.artcache = ArtworkCache(symbols=opts.symbols)
        self.spotify = self._open_spotify()
        self.library = Library(
            opts.path, spotify=self.spotify.source if self.spotify else None)
        self.local_player = Player(volume=opts.volume, sync_offset=opts.sync)
        self.player = self.local_player
        self.analysis = None
        self._own_analysis = True

        self.cursor = self._first_real()
        self.scroll = 0
        self.search_active = False
        self.search_query = ""
        self.show_help = False
        self.show_art = not opts.no_art and self.artcache.enabled

        self.current_path: str | None = None
        self.current_meta = None
        self.queue: list[str] = []
        self.queue_index = 0

        self.shuffle = False
        self.repeat = "off"
        self.autoplay = True

        self._await_listing = False   # navegación remota aún sin listado
        self.art_lines: list[str] | None = None
        self.art_failed = False
        self._art_dirty = True
        self._art_painted_at = 0.0
        self._full_repaint = True

        self.toast = ""
        self._toast_until = 0.0
        self.running = True

    def _first_real(self) -> int:
        """Primera entrada que no sea «..»: nadie quiere el cursor ahí."""
        entries = self.library.entries
        return 1 if entries and entries[0].is_parent and len(entries) > 1 else 0

    # -- Spotify -----------------------------------------------------------
    def _open_spotify(self) -> SpotifySession | None:
        if self.opts.no_spotify:
            return None
        session = SpotifySession(
            volume=self.opts.volume, audio=self.opts.spotify_audio,
            fifo=self.opts.spotify_fifo, lag=self.opts.spotify_lag,
            on_meta=self._remote_meta)
        return session if session.available else None

    def _remote_meta(self, meta) -> None:
        """El listado de Spotify ya trae metadatos: se guardan tal cual."""
        self.meta.put(meta)
        if meta.art_url:
            self.artcache.set_url(meta.path, meta.art_url)

    def toggle_source(self) -> None:
        if self.spotify is None:
            self.notify("Spotify no está conectado: nocturne --spotify-login")
            return
        if not self.library.toggle_source():
            self.notify("No se pudo cambiar de biblioteca")
            return
        self._await_listing = self.library.loading
        self.search_query = ""
        self.search_active = False
        self.scroll = 0
        self.cursor = self._first_real()
        self._full_repaint = True
        self.notify("Spotify" if self.library.on_spotify else "Archivos locales")

    # -- vistas ------------------------------------------------------------
    @property
    def view_entries(self):
        if not self.search_query:
            return self.library.entries
        needle = _fold(self.search_query)
        return [e for e in self.library.entries
                if e.is_parent or needle in _fold(e.name)]

    @property
    def queue_total(self) -> int:
        return len(self.queue)

    @property
    def autoplay_off(self) -> bool:
        return not self.autoplay

    @property
    def viz_position(self) -> float:
        return self.player.position

    def notify(self, message: str) -> None:
        self.toast = message
        self._toast_until = time.monotonic() + TOAST_SECONDS

    def clamp_scroll(self, viewport: int) -> None:
        total = len(self.view_entries)
        if total == 0:
            self.cursor = self.scroll = 0
            return
        self.cursor = max(0, min(total - 1, self.cursor))
        margin = 2 if viewport > 6 else 0
        if self.cursor < self.scroll + margin:
            self.scroll = max(0, self.cursor - margin)
        if self.cursor > self.scroll + viewport - 1 - margin:
            self.scroll = self.cursor - viewport + 1 + margin
        self.scroll = max(0, min(self.scroll, max(0, total - viewport)))

    # -- reproducción ------------------------------------------------------
    def build_queue(self, path: str) -> None:
        tracks = [e.path for e in self.library.tracks]
        if path not in tracks:
            tracks = [path]
        self.queue = tracks
        self.queue_index = tracks.index(path)

    def _use_player(self, remote: bool):
        """Cambia de motor de audio llevándose el volumen y el silencio."""
        target = self.spotify.player if (remote and self.spotify) else self.local_player
        if target is self.player:
            return target
        previous = self.player
        previous.stop()
        target.volume = previous.volume
        target.sync_offset = self.opts.sync
        if previous.muted and not target.muted:
            target.toggle_mute()
        elif target.muted and not previous.muted:
            target.toggle_mute()
        self.player = target
        return target

    def _set_analysis(self, analysis, owned: bool) -> None:
        """El analizador de streaming es de la sesión, no de la pista."""
        if self.analysis is not None and self._own_analysis:
            self.analysis.close()
        self.analysis = analysis
        self._own_analysis = owned

    def play_path(self, path: str, rebuild_queue: bool = True) -> None:
        remote = is_spotify(path)
        if remote and self.spotify is None:
            self.notify("Spotify no está conectado")
            return
        meta = self.meta.get_blocking(path)
        if meta.failed or not meta.duration:
            label = meta.display_title if remote else os.path.basename(path)
            _log(f"play_path RECHAZA {path} remoto={remote} "
                 f"fallo={meta.failed} dur={meta.duration}")
            self.notify(f"No se pudo leer «{label}»")
            return
        if rebuild_queue:
            self.build_queue(path)
        elif path in self.queue:
            self.queue_index = self.queue.index(path)

        player = self._use_player(remote)
        if remote:
            self.spotify.start_audio()
            if self.spotify.analysis is not None:
                self.spotify.analysis.silence()   # el AGC arranca de cero
                self._set_analysis(self.spotify.analysis, owned=False)
            else:
                self._set_analysis(None, owned=False)
        else:
            if self.spotify is not None:
                self.spotify.stop_audio()
            self._set_analysis(TrackAnalysis(path), owned=True)

        if remote:
            context = self.spotify.source.context_for(self.library.location)
            _log(f"play_path {path} ctx={context} offset={self.queue_index} "
                 f"cola={len(self.queue)} audio={self.spotify.audio_mode}")
            player.load(path, meta.duration, queue=self.queue,
                        offset=self.queue_index, context=context)
        else:
            player.load(path, meta.duration)
        self.current_path = path
        self.current_meta = meta
        self.art_lines = None
        self.art_failed = False
        self._art_dirty = True
        self._full_repaint = True      # borra la carátula anterior

    def play_index(self, index: int) -> None:
        if not self.queue:
            return
        index = max(0, min(len(self.queue) - 1, index))
        self.queue_index = index
        self.play_path(self.queue[index], rebuild_queue=False)

    def _step_remote(self, index: int) -> bool:
        """Avance a la pista contigua aprovechando la precarga de Spotify.

        Sólo vale para el siguiente elemento del contexto que ya está sonando:
        ahí librespot la tiene descargada y suena al momento. Cualquier otro
        salto (aleatorio, repetir una, cambio de carpeta) necesita un play
        nuevo y se va por `play_index`.
        """
        session = self.spotify
        if session is None or self.player is not session.player:
            return False
        if not self.player.is_active or index != self.queue_index + 1:
            return False
        if index >= len(self.queue) or not is_spotify(self.queue[index]):
            return False
        if not self.current_path or not is_spotify(self.current_path):
            return False
        path = self.queue[index]
        meta = self.meta.peek(path)
        if meta is None or not meta.duration:
            return False

        self.queue_index = index
        self.player.step_next(path, meta.duration)
        self.current_path = path
        self.current_meta = meta
        if session.analysis is not None:
            session.analysis.silence()   # el AGC arranca de cero
        self.art_lines = None
        self.art_failed = False
        self._art_dirty = True
        self._full_repaint = True
        return True

    def next_track(self, auto: bool = False) -> None:
        if not self.queue:
            return
        if auto and self.repeat == "one":
            self.play_index(self.queue_index)
            return
        if self.shuffle and len(self.queue) > 1:
            choice = random.randrange(len(self.queue) - 1)
            if choice >= self.queue_index:
                choice += 1
            self.play_index(choice)
            return
        nxt = self.queue_index + 1
        if nxt >= len(self.queue):
            if self.repeat == "all":
                nxt = 0
            else:
                self.player.stop()
                if auto:
                    self.notify("Fin de la carpeta")
                return
        if self._step_remote(nxt):
            return
        self.play_index(nxt)

    def prev_track(self) -> None:
        if not self.queue:
            return
        if self.player.is_active and self.player.position > 3.0:
            self.player.seek_to(0.0)
            return
        prev = self.queue_index - 1
        if prev < 0:
            prev = len(self.queue) - 1 if self.repeat == "all" else 0
        self.play_index(prev)

    def activate(self) -> None:
        """Intro / → sobre la selección: entra en la carpeta o suena la pista."""
        entries = self.view_entries
        if not entries:
            _log("activate sin entradas")
            return
        entry = entries[max(0, min(len(entries) - 1, self.cursor))]
        _log(f"activate cursor={self.cursor} «{entry.name}» dir={entry.is_dir}")
        if entry.is_dir:
            came_from = os.path.basename(self.library.directory)
            if self.library.cd(entry.path):
                self._await_listing = self.library.loading
                self.search_query = ""
                self.search_active = False
                self.scroll = 0
                self.cursor = self._first_real()
                if entry.is_parent:
                    idx = next((i for i, e in enumerate(self.library.entries)
                                if e.name == came_from), None)
                    if idx is not None:
                        self.cursor = idx
                self._full_repaint = True
            else:
                self.notify("No se pudo abrir la carpeta")
        else:
            self.play_path(entry.path)

    def go_up(self) -> None:
        came_from = os.path.basename(self.library.directory)
        if not self.library.up():
            return
        self._await_listing = self.library.loading
        self.search_query = ""
        self.search_active = False
        idx = next((i for i, e in enumerate(self.library.entries)
                    if e.name == came_from), None)
        self.cursor = idx if idx is not None else self._first_real()
        self.scroll = 0
        self._full_repaint = True

    # -- teclado -----------------------------------------------------------
    def handle_key(self, pressed) -> None:
        """`pressed` es un str (carácter escrito) o un int (tecla especial)."""
        if isinstance(pressed, str):
            char, key = pressed, ord(pressed)
        else:
            char, key = None, pressed

        if key == curses.KEY_RESIZE:
            self._full_repaint = True
            return
        if self.show_help:
            if key in (ord("?"), 27, ord("q"), ord(" ")):
                self.show_help = False
                self._full_repaint = True
            return
        if self.search_active and self._search_key(char, key):
            return

        entries = self.view_entries
        page = max(1, self.renderer.layout.library[2] - 4)

        if key in (curses.KEY_UP, ord("k")):
            self.cursor -= 1
        elif key in (curses.KEY_DOWN, ord("j")):
            self.cursor += 1
        elif key == curses.KEY_PPAGE:
            self.cursor -= page
        elif key == curses.KEY_NPAGE:
            self.cursor += page
        elif key in (curses.KEY_HOME, ord("g")):
            self.cursor = 0
        elif key in (curses.KEY_END, ord("G")):
            self.cursor = len(entries) - 1
        elif key in (curses.KEY_RIGHT, ord("l"), curses.KEY_ENTER, 10, 13):
            self.activate()
        elif key in (curses.KEY_LEFT, ord("h"), curses.KEY_BACKSPACE, 127, 8):
            self.go_up()
        elif key in (ord(" "), ord("p")):
            if self.player.is_active:
                self.player.toggle_pause()
            else:
                self.activate()
        elif key == ord("n"):
            self.next_track()
        elif key in (ord("N"), ord("b")):
            self.prev_track()
        elif key == ord("."):
            self.player.seek_relative(SEEK_SMALL)
        elif key == ord(","):
            self.player.seek_relative(-SEEK_SMALL)
        elif key in (ord(">"), curses.KEY_SRIGHT):
            self.player.seek_relative(SEEK_BIG)
        elif key in (ord("<"), curses.KEY_SLEFT):
            self.player.seek_relative(-SEEK_BIG)
        elif key in (ord("+"), ord("=")):
            self.player.change_volume(5)
        elif key in (ord("-"), ord("_")):
            self.player.change_volume(-5)
        elif key == ord("m"):
            self.player.toggle_mute()
        elif key == ord("s"):
            self.shuffle = not self.shuffle
            self.notify("Aleatorio activado" if self.shuffle else "Aleatorio desactivado")
        elif key == ord("r"):
            self.repeat = REPEAT_MODES[(REPEAT_MODES.index(self.repeat) + 1) % 3]
            self.notify({"off": "Sin repetición", "all": "Repetir la carpeta",
                         "one": "Repetir esta pista"}[self.repeat])
        elif key == ord("a"):
            self.autoplay = not self.autoplay
            self.notify("Encadena pistas" if self.autoplay else "Reproduce una y para")
        elif key == ord("v"):
            self.notify(f"Vista: {self.viz.cycle_mode(1)}")
        elif key == ord("V"):
            self.notify(f"Vista: {self.viz.cycle_mode(-1)}")
        elif key == ord("c"):
            self.show_art = not self.show_art and self.artcache.enabled
            self._full_repaint = True
            if not self.artcache.enabled:
                self.notify("chafa no está instalado")
        elif key == ord("S"):
            self.toggle_source()
        elif key == ord("d"):
            self.show_devices()
        elif key == ord("u"):
            self.library.refresh()
            self.notify("Carpeta recargada")
        elif key == ord("o"):
            self.library.show_all = not self.library.show_all
            self.library.refresh()
            self.notify("Mostrando todo" if self.library.show_all
                        else "Sólo archivos de audio")
        elif key == ord("/"):
            self.search_active = True
            self.search_query = ""
        elif key == ord("?"):
            self.show_help = True
            self._full_repaint = True
        elif key in (ord("0"), ord("1"), ord("2"), ord("3"), ord("4"),
                     ord("5"), ord("6"), ord("7"), ord("8"), ord("9")):
            if self.player.is_active and self.player.duration:
                self.player.seek_to(self.player.duration * (key - ord("0")) / 10.0)
        elif key == ord("q"):
            self.running = False
        elif key == 27:
            self.search_query = ""

    def _search_key(self, char: str | None, key: int) -> bool:
        """Devuelve True si la tecla la consume el buscador."""
        if key in (27,):
            self.search_active = False
            self.search_query = ""
            return True
        if key in (curses.KEY_ENTER, 10, 13):
            self.search_active = False
            if self.library.on_spotify and self.search_query.strip():
                query = self.search_query.strip()
                self.search_query = ""
                self.library.cd(spotify_source.search_location(query))
                self._await_listing = self.library.loading
                self.scroll = 0
                self.cursor = self._first_real()
                self._full_repaint = True
            return True
        if key in (curses.KEY_BACKSPACE, 127, 8):
            self.search_query = self.search_query[:-1]
            self.cursor = 0
            return True
        # Sólo caracteres reales: las teclas especiales llegan como int.
        if char is not None and char.isprintable():
            self.search_query += char
            self.cursor = 0
            return True
        return False

    # -- bucle -------------------------------------------------------------
    def update(self) -> None:
        if self.player.poll():
            if self.autoplay:
                self.next_track(auto=True)
            else:
                self.notify("Pista terminada")
        self._follow_remote()
        if self.library.poll():
            if self._await_listing:
                # El listado remoto llega después de navegar: hasta ahora el
                # cursor apuntaba a una lista vacía.
                self._await_listing = False
                self.cursor = self._first_real()
                self.scroll = 0
            self._full_repaint = True
        if self.player.error:
            _log(f"error del reproductor: {self.player.error}")
            self.notify(self.player.error)
            self.player.error = None
        if self.toast and time.monotonic() > self._toast_until:
            self.toast = ""
        self._prefetch_meta()
        self._update_art()

    def _prefetch_meta(self) -> None:
        entries = self.view_entries
        if not entries:
            return
        viewport = max(1, self.renderer.layout.library[2] - 2)
        start = max(0, self.scroll)
        for entry in entries[start:start + viewport + 12]:
            if not entry.is_dir:
                self.meta.get(entry.path)

    def _update_art(self) -> None:
        rect = self.renderer.layout.art
        if not self.show_art or rect is None or not self.current_path:
            if self.art_lines:
                self.art_lines = None
                self._full_repaint = True
            return
        _, _, h, w = rect
        lines = self.artcache.get(self.current_path, w, h)
        if lines and lines is not self.art_lines:
            self.art_lines = lines
            self._art_dirty = True
            self._full_repaint = True      # limpia el marcador «cargando…»
        self.art_failed = self.artcache.has_failed(self.current_path, w, h)

    def draw(self) -> None:
        height, width = self.scr.getmaxyx()
        layout = ui.compute_layout(height, width, self.show_art)
        if layout.art != self.renderer.layout.art:
            self._art_dirty = True
        self.renderer.layout = layout

        if self._full_repaint:
            self.scr.clear()
            self._art_dirty = True
            self._full_repaint = False
        else:
            self.scr.erase()

        if not layout.ok:
            self.renderer.draw_too_small(layout.message)
            self.scr.refresh()
            return

        self.renderer.draw_header(self)
        self.renderer.draw_library(self)
        self.renderer.draw_nowplaying(self)
        self.renderer.draw_visualizer(self)
        self.renderer.draw_transport(self)
        if self.show_help:
            self.renderer.draw_help(self)
        self.scr.refresh()

        if self.show_help or layout.art is None or not self.art_lines:
            return
        now = time.monotonic()
        if self._art_dirty or now - self._art_painted_at > ART_REPAINT_EVERY:
            self.renderer.paint_art(self.art_lines, layout.art)
            self._art_dirty = False
            self._art_painted_at = now

    def _read_key(self):
        """Devuelve str, int o None si expiró la espera."""
        try:
            return self.scr.get_wch()
        except curses.error:
            return None

    def run(self) -> None:
        curses.curs_set(0)
        self.scr.nodelay(False)
        while self.running:
            playing = self.player.state == PLAYING
            self.scr.timeout(int(1000 / self.opts.fps) if playing else 90)
            pressed = self._read_key()
            if pressed is not None:
                self.scr.timeout(0)
                for _ in range(24):          # vacía la ráfaga de autorepetición
                    self.handle_key(pressed)
                    if not self.running:
                        break
                    pressed = self._read_key()
                    if pressed is None:
                        break
            self.update()
            self.draw()

    def _follow_remote(self) -> None:
        """Spotify encadenó solo: Nocturne se pone al día sin dar otra orden."""
        player = self.player
        uri = getattr(player, "advanced_to", None)
        if not uri:
            return
        player.advanced_to = None
        self.current_path = uri
        if uri in self.queue:
            self.queue_index = self.queue.index(uri)
        meta = self.meta.peek(uri)
        if meta is None:
            meta = Meta(path=uri, duration=player.advanced_duration,
                        codec="spotify", loaded=True)
            self.meta.put(meta)
        self.current_meta = meta
        if self.spotify is not None and self.spotify.analysis is not None:
            self.spotify.analysis.silence()
        self.art_lines = None
        self.art_failed = False
        self._art_dirty = True
        self._full_repaint = True

    def show_devices(self) -> None:
        """Qué dispositivos de Spotify hay a la escucha ahora mismo."""
        if self.spotify is None:
            self.notify("Spotify no está conectado")
            return
        names = [d.get("name", "?") + (" ·activo" if d.get("is_active") else "")
                 for d in self.spotify.devices()]
        self.notify("Dispositivos: " + (", ".join(names) if names
                                        else "ninguno a la escucha"))

    def close(self) -> None:
        self.local_player.stop()
        if self.spotify is not None:
            self.spotify.player.stop()
            self.spotify.close()
        if self.analysis is not None and self._own_analysis:
            self.analysis.close()
        self.meta.close()
        self.artcache.close()


def _prepare_terminal() -> str | None:
    """Garantiza que la entrada y la salida sean un terminal real.

    Si vienen redirigidas (lanzado desde un prompt que captura la salida, con
    una tubería, etc.) se reabre /dev/tty, que es lo que curses necesita para
    poner el terminal en modo cbreak.
    """
    need_in = sys.stdin is None or not sys.stdin.isatty()
    need_out = sys.stdout is None or not sys.stdout.isatty()
    if not need_in and not need_out:
        return None
    try:
        fd = os.open("/dev/tty", os.O_RDWR)
    except OSError:
        return ("Nocturne necesita una terminal interactiva.\n"
                "Ábrelo en una ventana de terminal normal, no a través de una "
                "tubería ni desde el prompt de otra herramienta.")
    try:
        if need_in:
            os.dup2(fd, 0)
        if need_out:
            os.dup2(fd, 1)
    except OSError as exc:
        return f"No se pudo usar el terminal: {exc}"
    finally:
        if fd > 2:
            os.close(fd)
    return None


def _check_tools() -> list[str]:
    return [t for t in ("ffplay", "ffprobe", "ffmpeg") if shutil.which(t) is None]


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog="nocturne",
        description="Reproductor de música en la terminal con espectro ASCII y carátulas.")
    parser.add_argument("path", nargs="?", default=".",
                        help="carpeta (o archivo) con el que empezar; por defecto, la actual")
    parser.add_argument("-v", "--volume", type=int, default=80, metavar="0-100",
                        help="volumen inicial (por defecto 80)")
    parser.add_argument("--viz", choices=MODES, default="bars",
                        help="visualizador inicial")
    parser.add_argument("--no-art", action="store_true",
                        help="no mostrar carátulas aunque haya chafa")
    parser.add_argument("--symbols", default="vhalf+block+space",
                        help="juego de símbolos de chafa para la carátula")
    parser.add_argument("--fps", type=int, default=30, metavar="N",
                        help="refrescos por segundo del visualizador (por defecto 30)")
    parser.add_argument("--sync", type=float, default=0.0, metavar="SEG",
                        help="ajuste fino del espectro respecto al audio")

    spot = parser.add_argument_group("Spotify (requiere Premium)")
    spot.add_argument("--spotify-login", action="store_true",
                      help="autorizar Nocturne en tu cuenta y salir")
    spot.add_argument("--spotify-logout", action="store_true",
                      help="olvidar la autorización guardada y salir")
    spot.add_argument("--spotify-device-login", action="store_true",
                      help="autorizar el altavoz librespot (una sola vez) y salir")
    spot.add_argument("--spotify-client-id", default="", metavar="ID",
                      help="client id de tu app del panel de Spotify")
    spot.add_argument("--spotify", action="store_true",
                      help="arrancar directamente en el árbol de Spotify")
    spot.add_argument("--no-spotify", action="store_true",
                      help="no tocar Spotify aunque esté configurado")
    spot.add_argument("--spotify-audio", choices=("auto", "pipe", "loopback", "none"),
                      default="auto",
                      help="de dónde sale el PCM del espectro (por defecto: auto)")
    spot.add_argument("--spotify-fifo", default="", metavar="RUTA",
                      help="FIFO donde librespot escribe el PCM")
    spot.add_argument("--spotify-lag", type=float, default=0.22, metavar="SEG",
                      help="retraso del espectro respecto a lo que ya suena")
    opts = parser.parse_args(argv)
    opts.fps = max(5, min(60, opts.fps))
    opts.volume = max(0, min(100, opts.volume))
    return opts


def _spotify_cli(opts) -> int | None:
    """Atiende --spotify-login / --spotify-logout. None si no toca."""
    from .spotify import auth

    if opts.spotify_device_login:
        from .spotify import capture, device
        from .spotify.session import DEFAULT_FIFO

        if not device.installed():
            print("Primero instala librespot:  brew install librespot",
                  file=sys.stderr)
            return 1
        fifo = opts.spotify_fifo or DEFAULT_FIFO
        try:
            capture.make_fifo(fifo)
        except OSError as exc:
            print(f"No se pudo preparar el FIFO: {exc}", file=sys.stderr)
            return 1
        return device.login(fifo, volume=opts.volume)

    if opts.spotify_logout:
        auth.forget()
        print("Listo: Nocturne ya no guarda tu autorización de Spotify.")
        return 0
    if not opts.spotify_login:
        return None

    client_id = opts.spotify_client_id.strip() or auth.stored_client_id()
    if not client_id:
        print("Necesito el client id de una app tuya de Spotify.\n"
              "  1. Entra en https://developer.spotify.com/dashboard\n"
              "  2. Crea una app (el nombre da igual).\n"
              f"  3. Añade esta Redirect URI exacta:  {auth.REDIRECT_URI}\n"
              "  4. Vuelve aquí con:  nocturne --spotify-login --spotify-client-id ID",
              file=sys.stderr)
        return 1
    print("Abriendo el navegador para que autorices a Nocturne…")
    try:
        auth.authorize(client_id)
    except auth.AuthError as exc:
        print(f"No se pudo autorizar: {exc}", file=sys.stderr)
        return 1
    print("Hecho. Pulsa S dentro de Nocturne para saltar a Spotify.")
    return 0


def main(argv=None) -> int:
    opts = parse_args(argv)
    done = _spotify_cli(opts)
    if done is not None:
        return done
    missing = _check_tools()
    if missing:
        print(f"Faltan herramientas necesarias: {', '.join(missing)}.\n"
              f"Instálalas con:  brew install ffmpeg", file=sys.stderr)
        return 1

    start = os.path.abspath(os.path.expanduser(opts.path))
    autoplay_file = None
    if os.path.isfile(start):
        autoplay_file = start
        start = os.path.dirname(start)
    if not os.path.isdir(start):
        print(f"No existe la carpeta: {start}", file=sys.stderr)
        return 1
    opts.path = start

    problem = _prepare_terminal()
    if problem:
        print(problem, file=sys.stderr)
        return 1

    os.environ.setdefault("ESCDELAY", "25")

    def _run(stdscr):
        app = App(stdscr, opts)
        try:
            if opts.spotify and app.spotify is not None:
                app.toggle_source()
            if autoplay_file:
                app.play_path(autoplay_file)
                idx = app.library.index_of(autoplay_file)
                if idx is not None:
                    app.cursor = idx
            app.run()
        finally:
            app.close()

    try:
        curses.wrapper(_run)
    except KeyboardInterrupt:
        pass
    except curses.error as exc:
        print(f"El terminal no admite el modo que necesita Nocturne: {exc}\n"
              "Prueba en una ventana de terminal normal, con TERM bien "
              f"configurado (ahora TERM={os.environ.get('TERM', 'sin definir')}).",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
