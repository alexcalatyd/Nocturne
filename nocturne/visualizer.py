"""Visualizadores: espectro con reflejo, osciloscopio braille y modo dual."""
from __future__ import annotations

import curses
import math
import time

from . import theme as T

# Bloques anclados abajo: 1/8 .. 8/8.
LOWER = " ▁▂▃▄▅▆▇█"
BRAILLE_BASE = 0x2800
# Máscara de puntos braille por (columna, fila) dentro de la celda 2x4.
_DOTS = ((0x01, 0x02, 0x04, 0x40), (0x08, 0x10, 0x20, 0x80))

MODES = ("bars", "wave", "dual")
MODE_LABELS = {"bars": "espectro", "wave": "onda", "dual": "dual"}


class Braille:
    """Lienzo de puntos braille: 2x4 píxeles por celda de terminal."""

    def __init__(self, cols: int, rows: int) -> None:
        self.cols, self.rows = cols, rows
        self.px, self.py = cols * 2, rows * 4
        self.cells = [[0] * cols for _ in range(rows)]

    def point(self, x: int, y: int) -> None:
        if 0 <= x < self.px and 0 <= y < self.py:
            self.cells[y >> 2][x >> 1] |= _DOTS[x & 1][y & 3]

    def vline(self, x: int, y0: int, y1: int) -> None:
        if y0 > y1:
            y0, y1 = y1, y0
        y0 = max(0, y0)
        y1 = min(self.py - 1, y1)
        for y in range(y0, y1 + 1):
            self.point(x, y)

    def row_chars(self, row: int):
        """Devuelve (columna, carácter) de las celdas con puntos."""
        for col, mask in enumerate(self.cells[row]):
            if mask:
                yield col, chr(BRAILLE_BASE + mask)


class Visualizer:
    """Mantiene el suavizado entre frames y dibuja el modo activo."""

    def __init__(self, theme: T.Theme, mode: str = "bars") -> None:
        self.theme = theme
        self.mode = mode if mode in MODES else "bars"
        self.levels: list[float] = []
        self.peaks: list[float] = []
        self._peak_vel: list[float] = []
        self.energy = 0.0          # 0..1, alimenta los latidos del UI
        self._phase = 0.0
        self._last = time.monotonic()
        self._dim = 0

    def cycle_mode(self, step: int = 1) -> str:
        self.mode = MODES[(MODES.index(self.mode) + step) % len(MODES)]
        return self.mode

    # -- estado ------------------------------------------------------------
    def _resize(self, count: int) -> None:
        if len(self.levels) != count:
            self.levels = [0.0] * count
            self.peaks = [0.0] * count
            self._peak_vel = [0.0] * count

    def _advance(self, target: list[float] | None, dt: float) -> None:
        """Ataque rápido, caída lenta; los picos caen con aceleración."""
        steps = max(1, min(4, int(dt / 0.033 + 0.5)))
        for _ in range(steps):
            for i, cur in enumerate(self.levels):
                want = target[i] if target else 0.0
                cur += (want - cur) * (0.55 if want > cur else 0.20)
                if cur < 0.0005:
                    cur = 0.0
                self.levels[i] = cur
                peak = self.peaks[i]
                if cur >= peak:
                    self.peaks[i] = cur
                    self._peak_vel[i] = 0.0
                else:
                    self._peak_vel[i] += 0.0022
                    peak -= self._peak_vel[i]
                    self.peaks[i] = cur if peak < cur else peak
        if self.levels:
            self.energy = sum(self.levels) / len(self.levels)

    # -- dibujo ------------------------------------------------------------
    def draw(self, win, rect, analysis, position: float, playing: bool,
             paused: bool = False) -> None:
        y, x, h, w = rect
        if h < 2 or w < 4:
            return
        # En pausa el análisis sigue disponible: se dibuja el instante exacto
        # donde se detuvo, atenuado, en vez de dejar el panel vacío.
        self._dim = curses.A_DIM if paused else 0
        playing = playing or paused
        now = time.monotonic()
        dt = min(0.2, now - self._last)
        self._last = now
        self._phase += dt

        ready = analysis is not None and analysis.ready
        if self.mode == "bars":
            self._draw_bars(win, y, x, h, w, analysis, position, playing, ready, dt)
        elif self.mode == "wave":
            self._draw_wave(win, y, x, h, w, analysis, position, playing, ready)
        else:
            split = max(3, int(h * 0.58))
            self._draw_bars(win, y, x, split, w, analysis, position, playing, ready,
                            dt, mirror=False)
            self._draw_wave(win, y + split, x, h - split, w, analysis, position,
                            playing, ready)

    # -- espectro ----------------------------------------------------------
    def _draw_bars(self, win, y, x, h, w, analysis, position, playing, ready,
                   dt, mirror: bool = True) -> None:
        gap = 1
        bar_w = 2 if w >= 96 else 1
        stride = bar_w + gap
        count = max(1, (w + gap) // stride)
        self._resize(count)

        spectrum = None
        if ready and playing:
            spectrum = analysis.spectrum(position, count)
        self._advance(spectrum, dt)

        if not ready and not self._any_signal():
            self._draw_idle(win, y, x, h, w)
            return

        if mirror and h >= 5:
            top_h = max(2, int(h * 0.66))
            ref_h = h - top_h
        else:
            top_h = h
            ref_h = 0
        base = y + top_h - 1
        left = x + max(0, (w - (count * stride - gap)) // 2)

        for i in range(count):
            level = self.levels[i]
            col = left + i * stride
            if col + bar_w > x + w:
                break
            self._column(win, col, bar_w, base, top_h, level)
            if ref_h > 0:
                self._reflection(win, col, bar_w, base + 1, ref_h, level)
            if playing or level > 0.01:
                self._cap(win, col, bar_w, base, top_h, self.peaks[i], level)

    def _column(self, win, col, bar_w, base, top_h, level) -> None:
        total = level * top_h * 8.0
        full = int(total // 8)
        rem = int(total % 8)
        if full > top_h:
            full, rem = top_h, 0
        for i in range(full):
            frac = (i + 1) / top_h
            self._put(win, base - i, col, LOWER[8] * bar_w,
                      self.theme.ramp(T.SPECTRUM, frac))
        if rem and full < top_h:
            frac = (full + rem / 8.0) / top_h
            self._put(win, base - full, col, LOWER[rem] * bar_w,
                      self.theme.ramp(T.SPECTRUM, frac))

    def _reflection(self, win, col, bar_w, start, ref_h, level) -> None:
        total = level * 0.55 * ref_h * 8.0
        full = int(total // 8)
        rem = int(total % 8)
        if full > ref_h:
            full, rem = ref_h, 0
        for i in range(full):
            frac = 1.0 - (i + 1) / max(1, ref_h)
            self._put(win, start + i, col, LOWER[8] * bar_w,
                      self.theme.ramp(T.REFLECT, frac))
        if rem and full < ref_h:
            # Bloque anclado arriba: bloque inferior complementario en vídeo inverso.
            frac = 1.0 - (full + 1) / max(1, ref_h)
            attr = self.theme.ramp(T.REFLECT, max(0.0, frac)) | curses.A_REVERSE
            self._put(win, start + full, col, LOWER[8 - rem] * bar_w, attr)

    def _cap(self, win, col, bar_w, base, top_h, peak, level) -> None:
        if peak <= 0.02 or peak - level < 0.015:
            return
        rows_up = int(peak * top_h)
        if rows_up >= top_h:
            rows_up = top_h - 1
        row = base - rows_up
        if row < base - top_h + 1:
            return
        self._put(win, row, col, "▔" * bar_w,
                  self.theme.pair(T.ACCENT_SOFT) | curses.A_DIM)

    def _any_signal(self) -> bool:
        return any(v > 0.01 for v in self.levels)

    # -- osciloscopio ------------------------------------------------------
    def _draw_wave(self, win, y, x, h, w, analysis, position, playing, ready) -> None:
        if h < 1 or w < 2:
            return
        canvas = Braille(w, h)
        mid = canvas.py // 2
        amp = (canvas.py - 2) / 2.0

        data = None
        if ready and playing:
            data = analysis.waveform(position, canvas.px)
        if data:
            gain = 1.0 + 1.6 * (1.0 - min(1.0, max(abs(lo) for lo, _ in data) +
                                          max(hi for _, hi in data)))
            gain = max(1.0, min(2.6, gain))
            for px, (lo, hi) in enumerate(data):
                y0 = mid - int(hi * amp * gain)
                y1 = mid - int(lo * amp * gain)
                canvas.vline(px, y0, y1)
        else:
            self._idle_sine(canvas, mid, amp)

        for row in range(h):
            for col, ch in canvas.row_chars(row):
                dist = abs((row + 0.5) - h / 2.0) / max(0.5, h / 2.0)
                attr = self.theme.ramp(T.WAVE, dist)
                if not data:
                    attr |= curses.A_DIM
                self._put(win, y + row, x + col, ch, attr)

    def _idle_sine(self, canvas: Braille, mid: int, amp: float) -> None:
        for px in range(canvas.px):
            t = px / max(1, canvas.px - 1)
            value = (math.sin(t * math.tau * 1.5 - self._phase * 1.8) *
                     math.sin(t * math.pi) * 0.42)
            canvas.point(px, mid - int(value * amp))

    def _draw_idle(self, win, y, x, h, w) -> None:
        """Respiración suave mientras no hay audio decodificado."""
        canvas = Braille(w, h)
        self._idle_sine(canvas, canvas.py // 2, (canvas.py - 2) / 2.0)
        for row in range(h):
            for col, ch in canvas.row_chars(row):
                self._put(win, y + row, x + col, ch,
                          self.theme.pair(T.MUTED) | curses.A_DIM)

    def _put(self, win, y, x, text, attr) -> None:
        try:
            win.addstr(y, x, text, attr | self._dim)
        except curses.error:
            pass
