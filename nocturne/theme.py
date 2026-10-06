"""Paleta y atributos de color (256 colores, tonos azul noche)."""
from __future__ import annotations

import curses

# --- Paleta base (índices xterm-256) -------------------------------------
FRAME       = 238   # marcos inactivos
FRAME_ACTIVE= 67    # marco del panel enfocado
TITLE       = 111   # títulos de panel
ACCENT      = 39    # azul cielo, acento principal
ACCENT_SOFT = 74
TEXT        = 252
TEXT_STRONG = 255
DIM         = 245
MUTED       = 240
FAINT       = 236
FOLDER      = 109   # carpetas
PLAYING     = 45    # pista sonando
SEL_FG      = 231
SEL_BG      = 24    # azul oscuro de selección
BAR_BG      = 236
WARN        = 180

# Degradado del espectro: de azul profundo a cian pálido.
SPECTRUM = [18, 19, 20, 26, 32, 38, 39, 45, 51, 87, 123, 159, 195]
# Reflejo: la misma curva, apagada.
REFLECT = [17, 17, 18, 19, 25, 26, 31, 32, 38, 39, 44, 45]
# Onda (osciloscopio): del centro brillante a los bordes profundos.
WAVE = [45, 39, 38, 32, 26, 25]


class Theme:
    """Asigna pares de color bajo demanda y los cachea."""

    def __init__(self) -> None:
        self._pairs: dict[tuple[int, int], int] = {}
        self._next = 1
        self.has_color = False
        self.colors = 8

    def setup(self) -> None:
        try:
            curses.start_color()
            curses.use_default_colors()
            self.has_color = True
            self.colors = curses.COLORS
        except curses.error:
            self.has_color = False

    def pair(self, fg: int, bg: int = -1) -> int:
        """Devuelve el atributo curses para (fg, bg), creando el par si es nuevo."""
        if not self.has_color:
            return curses.A_NORMAL
        if self.colors < 256:
            fg = _downgrade(fg)
            bg = _downgrade(bg) if bg >= 0 else bg
        key = (fg, bg)
        got = self._pairs.get(key)
        if got is None:
            if self._next >= min(curses.COLOR_PAIRS, 255):
                return curses.A_NORMAL
            got = self._next
            self._next += 1
            try:
                curses.init_pair(got, fg, bg)
            except curses.error:
                return curses.A_NORMAL
            self._pairs[key] = got
        return curses.color_pair(got)

    def ramp(self, palette: list[int], frac: float) -> int:
        """Color del degradado `palette` para una fracción 0..1."""
        frac = 0.0 if frac < 0 else (1.0 if frac > 1 else frac)
        idx = int(frac * (len(palette) - 1) + 0.5)
        return self.pair(palette[idx])


def _downgrade(color: int) -> int:
    """Aproxima un índice 256 a los 8 colores básicos (terminales pobres)."""
    if color in (231, 255, 252, 195, 159):
        return curses.COLOR_WHITE
    if color in (238, 240, 236, 245, 17, 18, 19, 20):
        return curses.COLOR_BLUE
    if color in (123, 87, 51, 45, 44):
        return curses.COLOR_CYAN
    return curses.COLOR_BLUE
