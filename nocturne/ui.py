"""Dibujo de la interfaz en curses."""
from __future__ import annotations

import curses
import os
import sys
from dataclasses import dataclass

from . import theme as T
from .player import PAUSED, PLAYING
from .util import fit, fmt_time, pad, truncate, text_width
from .visualizer import MODE_LABELS

MIN_W, MIN_H = 58, 16

ICON_PLAY = "▶"
ICON_PAUSE = "‖"
ICON_NOTE = "♪"
ICON_DIR = "▸"
ICON_UP = "↑"

Rect = tuple[int, int, int, int]      # (y, x, alto, ancho)


@dataclass
class Layout:
    ok: bool = True
    message: str = ""
    header: Rect = (0, 0, 0, 0)
    library: Rect = (0, 0, 0, 0)
    nowplaying: Rect = (0, 0, 0, 0)
    visualizer: Rect = (0, 0, 0, 0)
    transport: Rect = (0, 0, 0, 0)
    art: Rect | None = None
    meta: Rect | None = None


def compute_layout(height: int, width: int, want_art: bool) -> Layout:
    if width < MIN_W or height < MIN_H:
        return Layout(ok=False,
                      message=f"Terminal demasiado pequeña ({width}x{height}). "
                              f"Mínimo {MIN_W}x{MIN_H}.")
    lay = Layout()
    lay.header = (0, 0, 1, width)
    transport_h = 3
    body_y = 1
    body_h = height - 1 - transport_h
    lay.transport = (height - transport_h, 0, transport_h, width)

    lib_w = max(24, min(44, int(width * 0.34)))
    if width < 76:
        lib_w = max(20, int(width * 0.38))
    right_x = lib_w + 1
    right_w = width - right_x

    lay.library = (body_y, 0, body_h, lib_w)

    # Panel de reproducción: alto según lo que quepa, la carátula manda.
    art_h = max(4, min(13, (body_h - 6) // 2))
    art_w = art_h * 2
    show_art = want_art and right_w >= art_w + 22 and body_h >= 14
    if not show_art:
        art_h = min(4, max(3, body_h // 5))
        art_w = 0
    np_h = art_h + 2
    if np_h > body_h - 5:
        np_h = max(5, body_h - 5)
        art_h = np_h - 2
        art_w = art_h * 2 if show_art else 0

    lay.nowplaying = (body_y, right_x, np_h, right_w)
    lay.visualizer = (body_y + np_h, right_x, body_h - np_h, right_w)

    inner_y, inner_x = body_y + 1, right_x + 2
    if show_art:
        lay.art = (inner_y, inner_x, art_h, art_w)
        meta_x = inner_x + art_w + 2
        lay.meta = (inner_y, meta_x, art_h, max(0, right_x + right_w - 2 - meta_x))
    else:
        lay.art = None
        lay.meta = (inner_y, inner_x, np_h - 2, right_w - 4)
    return lay


class Renderer:
    """Pinta cada panel a partir del estado de la aplicación."""

    def __init__(self, stdscr, theme: T.Theme) -> None:
        self.scr = stdscr
        self.theme = theme
        self.layout = Layout()

    # -- primitivas --------------------------------------------------------
    def put(self, y: int, x: int, text: str, attr=curses.A_NORMAL) -> None:
        if not text:
            return
        h, w = self.scr.getmaxyx()
        if y < 0 or y >= h or x >= w:
            return
        if x < 0:
            text = text[-x:]
            x = 0
        room = w - x
        if room <= 0:
            return
        text = truncate(text, room, "")
        try:
            self.scr.addstr(y, x, text, attr)
        except curses.error:
            pass    # la esquina inferior derecha siempre protesta

    def box(self, rect: Rect, title: str = "", active: bool = False,
            badge: str = "") -> None:
        y, x, h, w = rect
        if h < 2 or w < 2:
            return
        frame = self.theme.pair(T.FRAME_ACTIVE if active else T.FRAME)
        top = "╭" + "─" * (w - 2) + "╮"
        self.put(y, x, top, frame)
        for row in range(1, h - 1):
            self.put(y + row, x, "│", frame)
            self.put(y + row, x + w - 1, "│", frame)
        self.put(y + h - 1, x, "╰" + "─" * (w - 2) + "╯", frame)
        if title:
            label = f" {title} "
            self.put(y, x + 2, label,
                     self.theme.pair(T.TITLE) |
                     (curses.A_BOLD if active else curses.A_NORMAL))
        if badge:
            label = f" {badge} "
            bx = x + w - 2 - text_width(label)
            if bx > x + 2 + text_width(title) + 3:
                self.put(y, bx, label, self.theme.pair(T.MUTED))

    # -- paneles -----------------------------------------------------------
    def draw_header(self, app) -> None:
        y, x, _, w = self.layout.header
        self.put(y, 0, " " * w, self.theme.pair(T.TEXT, T.FAINT))
        brand = f" {ICON_NOTE} NOCTURNE "
        self.put(y, 0, brand, self.theme.pair(T.ACCENT, T.FAINT) | curses.A_BOLD)

        state = app.player.state
        if state == PLAYING:
            badge, color = f"{ICON_PLAY} sonando", T.PLAYING
        elif state == PAUSED:
            badge, color = f"{ICON_PAUSE} en pausa", T.WARN
        else:
            badge, color = "detenido", T.MUTED
        badge = f" {badge} "
        bx = max(0, w - text_width(badge) - 1)
        self.put(y, bx, badge, self.theme.pair(color, T.FAINT))

        path = app.library.title
        room = bx - text_width(brand) - 3
        if room > 6:
            shown = path
            if text_width(shown) > room:
                shown = "…" + shown[-(room - 1):]
            self.put(y, text_width(brand) + 1, shown,
                     self.theme.pair(T.DIM, T.FAINT))

    def draw_library(self, app) -> None:
        rect = self.layout.library
        y, x, h, w = rect
        count = len(app.view_entries)
        badge = f"{count} elem." if count else ""
        if app.search_active:
            title = "BUSCAR EN SPOTIFY" if app.library.on_spotify else "BUSCAR"
        else:
            title = "SPOTIFY" if app.library.on_spotify else "BIBLIOTECA"
        self.box(rect, title, active=True, badge=badge)

        inner_y, inner_x = y + 1, x + 2
        inner_h, inner_w = h - 2, w - 4
        if inner_h <= 0 or inner_w <= 2:
            return

        if app.library.error:
            self.put(inner_y, inner_x, truncate(app.library.error, inner_w),
                     self.theme.pair(T.WARN))
            return
        if app.library.loading and not app.view_entries:
            self.put(inner_y, inner_x, "cargando desde Spotify…",
                     self.theme.pair(T.MUTED) | curses.A_DIM)
            return
        if not app.view_entries:
            msg = "sin resultados" if app.search_query else "aquí no hay nada"
            self.put(inner_y, inner_x, msg, self.theme.pair(T.MUTED) | curses.A_DIM)
            return

        app.clamp_scroll(inner_h)
        top = app.scroll
        visible = app.view_entries[top:top + inner_h]
        show_scrollbar = count > inner_h
        list_w = inner_w - (1 if show_scrollbar else 0)

        for row, entry in enumerate(visible):
            idx = top + row
            selected = idx == app.cursor
            playing = (app.current_path == entry.path and not entry.is_dir)
            line_y = inner_y + row

            if entry.is_parent:
                icon, color = ICON_UP, T.FOLDER
            elif entry.is_dir:
                icon, color = ICON_DIR, T.FOLDER
            elif playing:
                icon = ICON_PAUSE if app.player.state == PAUSED else ICON_PLAY
                color = T.PLAYING
            else:
                icon, color = ICON_NOTE, T.TEXT

            attr = self.theme.pair(color)
            if playing:
                attr |= curses.A_BOLD
            if selected:
                attr = self.theme.pair(T.SEL_FG, T.SEL_BG) | curses.A_BOLD
                self.put(line_y, inner_x - 1, " " * (list_w + 2),
                         self.theme.pair(T.SEL_FG, T.SEL_BG))

            tail = ""
            if not entry.is_dir:
                meta = app.meta.get(entry.path)
                if meta and meta.duration:
                    tail = fmt_time(meta.duration)
            name_w = list_w - 2 - (text_width(tail) + 1 if tail else 0)
            self.put(line_y, inner_x, f"{icon} ", attr)
            self.put(line_y, inner_x + 2, fit(entry.name, max(0, name_w)), attr)
            if tail:
                tail_attr = attr if selected else self.theme.pair(T.MUTED)
                self.put(line_y, inner_x + list_w - text_width(tail), tail, tail_attr)

        if show_scrollbar:
            self._scrollbar(inner_y, inner_x + inner_w - 1, inner_h, count,
                            top, len(visible))

    def _scrollbar(self, y, x, h, total, top, visible) -> None:
        track = self.theme.pair(T.FAINT)
        thumb = self.theme.pair(T.FRAME_ACTIVE)
        size = max(1, int(h * visible / max(1, total)))
        pos = int(h * top / max(1, total))
        pos = min(pos, h - size)
        for row in range(h):
            inside = pos <= row < pos + size
            self.put(y + row, x, "┃" if inside else "│", thumb if inside else track)

    def draw_nowplaying(self, app) -> None:
        rect = self.layout.nowplaying
        meta_rect = self.layout.meta
        track = app.current_meta
        badge = ""
        if app.queue_total:
            badge = f"{app.queue_index + 1}/{app.queue_total}"
        self.box(rect, "REPRODUCIENDO", badge=badge)
        if meta_rect is None:
            return
        my, mx, mh, mw = meta_rect
        if mw <= 4:
            return

        if track is None:
            self.put(my + max(0, mh // 2 - 1), mx,
                     truncate("Nada sonando todavía", mw),
                     self.theme.pair(T.DIM))
            self.put(my + max(1, mh // 2), mx,
                     truncate("Elige una pista y pulsa Intro", mw),
                     self.theme.pair(T.MUTED) | curses.A_DIM)
            return

        rows = []
        rows.append((track.display_title, self.theme.pair(T.TEXT_STRONG) | curses.A_BOLD))
        rows.append((track.display_artist, self.theme.pair(T.ACCENT)))
        album = track.album
        if album and track.date:
            album = f"{album} · {track.date[:4]}"
        elif track.date:
            album = track.date[:4]
        if album:
            rows.append((album, self.theme.pair(T.DIM)))
        if track.genre:
            rows.append((track.genre, self.theme.pair(T.MUTED) | curses.A_DIM))
        if mh >= 5 and track.quality:
            rows.append(("", curses.A_NORMAL))
            rows.append((track.quality, self.theme.pair(T.MUTED) | curses.A_DIM))

        start = my + max(0, (mh - len(rows)) // 2)
        for i, (text, attr) in enumerate(rows):
            if start + i >= my + mh:
                break
            self.put(start + i, mx, truncate(text, mw), attr)

        if self.layout.art is None:
            return
        # Marco tenue alrededor del hueco reservado a la carátula.
        ay, ax, ah, aw = self.layout.art
        if not app.art_lines:
            self._art_placeholder(ay, ax, ah, aw, app)

    def _art_placeholder(self, y, x, h, w, app) -> None:
        dim = self.theme.pair(T.FRAME) | curses.A_DIM
        for row in range(h):
            self.put(y + row, x, "·" * w if row in (0, h - 1) else
                     "·" + " " * (w - 2) + "·", dim)
        if h >= 3:
            note = "sin carátula" if app.art_failed else "cargando…"
            self.put(y + h // 2 - 1, x + max(0, (w - 1) // 2), ICON_NOTE,
                     self.theme.pair(T.FRAME_ACTIVE))
            self.put(y + h // 2 + 1, x + max(0, (w - text_width(note)) // 2),
                     truncate(note, w), self.theme.pair(T.MUTED) | curses.A_DIM)

    def draw_visualizer(self, app) -> None:
        rect = self.layout.visualizer
        y, x, h, w = rect
        if h < 3:
            return
        self.box(rect, "ESPECTRO", badge=MODE_LABELS.get(app.viz.mode, ""))
        inner = (y + 1, x + 2, h - 2, w - 4)
        if inner[2] < 1 or inner[3] < 4:
            return
        app.viz.draw(self.scr, inner, app.analysis, app.viz_position,
                     app.player.state == PLAYING,
                     paused=app.player.state == PAUSED)

    def draw_transport(self, app) -> None:
        y, x, h, w = self.layout.transport
        player = app.player
        pad_x = 2
        inner_w = w - pad_x * 2

        # -- fila 1: barra de progreso con tiempos
        pos = player.position if player.is_active else 0.0
        dur = player.duration or (app.current_meta.duration if app.current_meta else 0.0)
        left = fmt_time(pos if player.is_active else None)
        right = fmt_time(dur if dur else None)
        bar_w = inner_w - text_width(left) - text_width(right) - 2
        if bar_w > 4:
            frac = (pos / dur) if dur > 0 else 0.0
            frac = max(0.0, min(1.0, frac))
            filled = int(bar_w * frac)
            bx = x + pad_x + text_width(left) + 1
            self.put(y, x + pad_x, left, self.theme.pair(T.DIM))
            self.put(y, bx, "━" * filled, self.theme.pair(T.ACCENT))
            self.put(y, bx + filled, "─" * (bar_w - filled),
                     self.theme.pair(T.FAINT))
            if player.is_active:
                knob = min(bar_w - 1, filled)
                self.put(y, bx + knob, "●",
                         self.theme.pair(T.TEXT_STRONG) | curses.A_BOLD)
            self.put(y, bx + bar_w + 1, right, self.theme.pair(T.DIM))

        # -- fila 2: modos y volumen
        badges = []
        if app.shuffle:
            badges.append(("⤨ aleatorio", T.ACCENT))
        if app.repeat == "all":
            badges.append(("↻ repetir todo", T.ACCENT))
        elif app.repeat == "one":
            badges.append(("↻ repetir una", T.ACCENT))
        if app.autoplay_off:
            badges.append(("· una y para", T.MUTED))
        cx = x + pad_x
        for label, color in badges:
            self.put(y + 1, cx, label, self.theme.pair(color))
            cx += text_width(label) + 3

        vol_cells = 10
        filled = int(round(player.volume / 100 * vol_cells))
        vol_txt = f"{player.volume:3d}%"
        vol_w = vol_cells + text_width(vol_txt) + 3
        vx = x + w - pad_x - vol_w
        if vx > cx:
            icon_attr = self.theme.pair(T.MUTED if player.muted else T.ACCENT)
            self.put(y + 1, vx, "🔇" if False else ("♪" if not player.muted else "✕"),
                     icon_attr)
            self.put(y + 1, vx + 2, "▰" * filled, self.theme.pair(T.ACCENT))
            self.put(y + 1, vx + 2 + filled, "▱" * (vol_cells - filled),
                     self.theme.pair(T.FAINT))
            self.put(y + 1, vx + 3 + vol_cells, vol_txt, self.theme.pair(T.DIM))

        # -- fila 3: pistas de teclado, aviso o buscador
        self.draw_hints(app, y + 2, x + pad_x, inner_w)

    def draw_hints(self, app, y: int, x: int, w: int) -> None:
        if app.search_active:
            label = "buscar ▸ "
            self.put(y, x, label, self.theme.pair(T.ACCENT) | curses.A_BOLD)
            self.put(y, x + text_width(label), truncate(app.search_query, w - 12),
                     self.theme.pair(T.TEXT_STRONG))
            self.put(y, x + text_width(label) + text_width(app.search_query), "▁",
                     self.theme.pair(T.ACCENT) | curses.A_BLINK)
            return
        if app.toast:
            self.put(y, x, truncate(app.toast, w),
                     self.theme.pair(T.WARN) | curses.A_BOLD)
            return
        hints = ("espacio sonar · ↑↓ mover · → abrir · ← atrás · ,. saltar · "
                 "n/N pista · S spotify · v vista · / buscar · ? ayuda · q salir")
        self.put(y, x, truncate(hints, w), self.theme.pair(T.MUTED) | curses.A_DIM)

    # -- superposiciones ---------------------------------------------------
    def draw_help(self, app) -> None:
        h, w = self.scr.getmaxyx()
        rows = [
            ("Reproducción", ""),
            ("espacio / p", "reproducir o pausar"),
            ("Intro", "abrir carpeta o sonar pista"),
            ("n / N", "pista siguiente / anterior"),
            (". , (o → ←)", "saltar ±5 s  ·  con Mayús ±30 s"),
            ("s", "aleatorio"),
            ("r", "repetir: no / todo / una"),
            ("a", "encadenar pistas al terminar"),
            ("", ""),
            ("Navegación", ""),
            ("↑ ↓ / j k", "mover la selección"),
            ("→ / l", "entrar en la carpeta"),
            ("← / h", "volver a la carpeta anterior"),
            ("g / G", "principio / final de la lista"),
            ("AvPág RePág", "página arriba / abajo"),
            ("/", "buscar en la carpeta (Esc limpia)"),
            ("u", "recargar la carpeta"),
            ("o", "mostrar también archivos no musicales"),
            ("", ""),
            ("Sonido y vista", ""),
            ("+ / -", "volumen ±5"),
            ("m", "silenciar"),
            ("v / V", "cambiar de visualizador"),
            ("c", "mostrar u ocultar la carátula"),
            ("?", "esta ayuda"),
            ("q", "salir"),
            ("", ""),
            ("Spotify", ""),
            ("S", "saltar entre tus archivos y Spotify"),
            ("/", "en Spotify, Intro busca en el catálogo"),
            ("d", "qué dispositivos hay a la escucha"),
        ]
        bw = min(w - 4, 54)
        bh = min(h - 2, len(rows) + 4)
        by = (h - bh) // 2
        bx = (w - bw) // 2
        for row in range(bh):
            self.put(by + row, bx, " " * bw, self.theme.pair(T.TEXT, T.FAINT))
        self.box((by, bx, bh, bw), "AYUDA", active=True, badge="? cierra")
        inner = bh - 3
        for i, (key, desc) in enumerate(rows[:inner]):
            ry = by + 2 + i
            if not desc:
                self.put(ry, bx + 3, key,
                         self.theme.pair(T.ACCENT, T.FAINT) | curses.A_BOLD)
                continue
            self.put(ry, bx + 3, pad(key, 13),
                     self.theme.pair(T.TEXT_STRONG, T.FAINT))
            self.put(ry, bx + 16, truncate(desc, bw - 19),
                     self.theme.pair(T.DIM, T.FAINT))

    def draw_too_small(self, message: str) -> None:
        h, w = self.scr.getmaxyx()
        self.put(h // 2, max(0, (w - text_width(message)) // 2),
                 truncate(message, w), self.theme.pair(T.WARN))

    # -- carátula fuera de curses -----------------------------------------
    def paint_art(self, lines: list[str], rect: Rect) -> None:
        """Escribe el bloque ANSI de chafa directamente en el terminal.

        curses nunca dibuja en ese rectángulo, así que su modelo de pantalla
        lo cree en blanco y no lo sobrescribe hasta un repintado completo.
        """
        y, x, h, w = rect
        out = ["\x1b[s"]                       # guarda la posición del cursor
        for row, line in enumerate(lines[:h]):
            out.append(f"\x1b[{y + row + 1};{x + 1}H{line}\x1b[0m")
        out.append("\x1b[u")                   # y la restaura
        try:
            sys.stdout.write("".join(out))
            sys.stdout.flush()
        except (BrokenPipeError, ValueError):
            pass
