"""Análisis de audio: PCM cacheado + FFT en Python puro.

La pista se decodifica entera a PCM mono 16-bit (ffmpeg tarda ~0.1 s por
canción) y se mapea en memoria. Así el espectro se lee exactamente en la
posición de reproducción, sin desfase acumulado.
"""
from __future__ import annotations

import cmath
import math
import mmap
import os
import struct
import subprocess
import tempfile
import threading

SAMPLE_RATE = 22050
FFT_SIZE = 2048
_HALF = FFT_SIZE // 2

# Twiddles y ventana de Hann precalculados una sola vez.
_TWIDDLE = [cmath.exp(-2j * math.pi * k / FFT_SIZE) for k in range(_HALF)]
_HANN = [0.5 - 0.5 * math.cos(2 * math.pi * i / (FFT_SIZE - 1)) for i in range(FFT_SIZE)]
_UNPACK = struct.Struct(f"<{FFT_SIZE}h").unpack_from

MIN_HZ, MAX_HZ = 40.0, 10000.0
# Rango útil tras normalizar la magnitud por FFT_SIZE/4 (0 dB = fondo de escala).
FLOOR_DB, CEIL_DB = -52.0, -2.0
_MAG_NORM = 4.0 / FFT_SIZE
# Límites del control automático de ganancia, en dB.
AGC_MIN, AGC_MAX = -15.0, 18.0


def _fft(a: list[complex]) -> list[complex]:
    """FFT radix-2 in-place (decimación en tiempo, iterativa)."""
    n = len(a)
    j = 0
    for i in range(1, n):
        bit = n >> 1
        while j & bit:
            j ^= bit
            bit >>= 1
        j |= bit
        if i < j:
            a[i], a[j] = a[j], a[i]
    length = 2
    while length <= n:
        step = n // length
        half = length // 2
        for start in range(0, n, length):
            k = 0
            for m in range(start, start + half):
                t = _TWIDDLE[k] * a[m + half]
                u = a[m]
                a[m] = u + t
                a[m + half] = u - t
                k += step
        length <<= 1
    return a


class _Spectra:
    """FFT, bandas, AGC y onda sobre una ventana de muestras cualquiera.

    Las subclases sólo tienen que saber devolver `size` muestras: de un
    fichero ya decodificado (`TrackAnalysis`) o de lo que esté sonando ahora
    mismo (`StreamAnalysis`).
    """

    def _init_spectra(self) -> None:
        self._band_cache: dict[int, list[tuple[int, int]]] = {}
        self._agc_db = 0.0

    def _window_at(self, position: float, size: int = FFT_SIZE) -> tuple | None:
        raise NotImplementedError

    def _bands(self, count: int) -> list[tuple[int, int]]:
        """Rangos de bins FFT por barra, espaciados logarítmicamente."""
        got = self._band_cache.get(count)
        if got is not None:
            return got
        bin_hz = SAMPLE_RATE / FFT_SIZE
        ratio = MAX_HZ / MIN_HZ
        bands, prev = [], None
        for i in range(count):
            lo_hz = MIN_HZ * ratio ** (i / count)
            hi_hz = MIN_HZ * ratio ** ((i + 1) / count)
            lo = int(lo_hz / bin_hz)
            hi = max(lo + 1, int(hi_hz / bin_hz))
            if prev is not None and lo < prev:
                lo = prev
                hi = max(lo + 1, hi)
            hi = min(hi, _HALF - 1)
            lo = min(lo, hi - 1) if hi > 1 else 1
            bands.append((max(1, lo), hi))
            prev = hi
        self._band_cache[count] = bands
        return bands

    def spectrum(self, position: float, count: int) -> list[float] | None:
        """Magnitudes 0..1 por banda, ya en escala dB e inclinadas en agudos."""
        raw = self._window_at(position)
        if raw is None or count <= 0:
            return None
        buf = [complex(raw[i] * _HANN[i] * (1.0 / 32768.0), 0.0) for i in range(FFT_SIZE)]
        spec = _fft(buf)
        mags = [abs(spec[i]) for i in range(_HALF)]

        raw_db = []
        for idx, (lo, hi) in enumerate(self._bands(count)):
            chunk = mags[lo:hi]
            if not chunk:
                raw_db.append(FLOOR_DB - 20.0)
                continue
            peak = max(chunk)
            mean = sum(chunk) / len(chunk)
            value = (peak * 0.7 + mean * 0.3) * _MAG_NORM
            db = 20.0 * math.log10(value + 1e-9)
            # Compensa la caída natural de energía en agudos.
            db += 11.0 * (idx / max(1, count - 1))
            raw_db.append(db)

        gain = self._update_agc(max(raw_db))
        span = CEIL_DB - FLOOR_DB
        out = []
        for db in raw_db:
            norm = (db + gain - FLOOR_DB) / span
            out.append(0.0 if norm < 0.0 else (1.0 if norm > 1.0 else norm))
        return out

    def _update_agc(self, frame_peak_db: float) -> float:
        """Ajusta la ganancia para que los picos rocen el techo, sin bombeo."""
        target = (CEIL_DB - 3.0) - frame_peak_db
        if target < self._agc_db:
            step = 3.0            # bajar rápido: evita saturar
        elif frame_peak_db > FLOOR_DB + 10.0:
            step = 0.25           # subir despacio, y sólo si hay señal real
        else:
            step = 0.0            # silencio: congela la ganancia
        delta = target - self._agc_db
        if delta > step:
            delta = step
        elif delta < -step:
            delta = -step
        self._agc_db += delta
        if self._agc_db < AGC_MIN:
            self._agc_db = AGC_MIN
        elif self._agc_db > AGC_MAX:
            self._agc_db = AGC_MAX
        return self._agc_db

    def waveform(self, position: float, columns: int, span_s: float = 0.055):
        """Pares (min, max) normalizados -1..1 por columna, para el osciloscopio."""
        if columns <= 0:
            return None
        size = max(256, min(8192, int(span_s * SAMPLE_RATE)))
        raw = self._window_at(position, size)
        if raw is None:
            return None
        per = max(1, len(raw) // columns)
        out = []
        for c in range(columns):
            seg = raw[c * per:(c + 1) * per]
            if not seg:
                out.append((0.0, 0.0))
                continue
            out.append((min(seg) / 32768.0, max(seg) / 32768.0))
        return out

    def rms(self, position: float) -> float:
        """Nivel medio (0..1) alrededor de la posición: alimenta el latido del UI."""
        raw = self._window_at(position, 1024)
        if not raw:
            return 0.0
        acc = sum(s * s for s in raw) / len(raw)
        return min(1.0, math.sqrt(acc) / 9000.0)

class TrackAnalysis(_Spectra):
    """Decodifica una pista en segundo plano y sirve espectro/onda."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.ready = False
        self.failed = False
        self._mm: mmap.mmap | None = None
        self._fh = None
        self._tmp: str | None = None
        self._samples = 0
        self._lock = threading.Lock()
        self._closed = False
        self._init_spectra()
        self._thread = threading.Thread(target=self._decode, daemon=True)
        self._thread.start()

    # -- ciclo de vida -----------------------------------------------------
    def _decode(self) -> None:
        fd, tmp = tempfile.mkstemp(prefix="nocturne-", suffix=".raw")
        os.close(fd)
        try:
            proc = subprocess.run(
                ["ffmpeg", "-v", "quiet", "-y", "-i", self.path,
                 "-f", "s16le", "-acodec", "pcm_s16le",
                 "-ac", "1", "-ar", str(SAMPLE_RATE), tmp],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, timeout=120, start_new_session=True,
            )
            size = os.path.getsize(tmp) if proc.returncode == 0 else 0
            if size < FFT_SIZE * 2:
                raise RuntimeError("sin PCM utilizable")
            fh = open(tmp, "rb")
            mm = mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ)
            with self._lock:
                if self._closed:
                    mm.close(); fh.close(); os.unlink(tmp)
                    return
                self._fh, self._mm, self._tmp = fh, mm, tmp
                self._samples = size // 2
                self.ready = True
        except Exception:
            self.failed = True
            try:
                os.unlink(tmp)
            except OSError:
                pass

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self.ready = False
            if self._mm is not None:
                try:
                    self._mm.close()
                except (BufferError, ValueError):
                    pass
            if self._fh is not None:
                try:
                    self._fh.close()
                except OSError:
                    pass
            if self._tmp:
                try:
                    os.unlink(self._tmp)
                except OSError:
                    pass
            self._mm = self._fh = self._tmp = None

    # -- lectura -----------------------------------------------------------
    def _window_at(self, position: float, size: int = FFT_SIZE) -> tuple | None:
        """Devuelve `size` muestras centradas en `position` (segundos)."""
        with self._lock:
            mm, total = self._mm, self._samples
            if mm is None or total < size:
                return None
            start = int(position * SAMPLE_RATE) - size // 2
            start = max(0, min(total - size, start))
            try:
                if size == FFT_SIZE:
                    return _UNPACK(mm, start * 2)
                return struct.unpack_from(f"<{size}h", mm, start * 2)
            except (ValueError, struct.error):
                return None



class StreamAnalysis(_Spectra):
    """Espectro de lo que está sonando ahora, sin fichero que decodificar.

    Un búfer circular guarda los últimos segundos de PCM mono a `SAMPLE_RATE`.
    Como el audio se oye con unos milisegundos de retraso respecto a lo que ya
    hemos escrito, la ventana se lee `lag` segundos por detrás de la cabeza.
    """

    BUFFER_SECONDS = 6.0

    def __init__(self, lag: float = 0.0) -> None:
        self._init_spectra()
        self.ready = False
        self.failed = False
        self.lag = max(0.0, lag)
        self._size = int(SAMPLE_RATE * self.BUFFER_SECONDS)
        self._buf = bytearray(self._size * 2)
        self._head = 0               # índice de muestra donde se escribirá
        self._filled = 0             # muestras válidas acumuladas
        self._lock = threading.Lock()
        self._closed = False

    # -- escritura ---------------------------------------------------------
    def feed(self, chunk: bytes) -> None:
        """Añade PCM mono s16le. Lo llama el hilo de captura."""
        if not chunk or self._closed:
            return
        count = len(chunk) // 2
        if count == 0:
            return
        if count >= self._size:
            chunk = chunk[-self._size * 2:]
            count = self._size
        with self._lock:
            head = self._head
            end = head + count
            if end <= self._size:
                self._buf[head * 2:end * 2] = chunk
            else:
                split = (self._size - head) * 2
                self._buf[head * 2:] = chunk[:split]
                self._buf[:(end - self._size) * 2] = chunk[split:]
            self._head = end % self._size
            self._filled = min(self._size, self._filled + count)
            self.ready = self._filled >= FFT_SIZE

    def silence(self) -> None:
        """Olvida lo acumulado: al parar, el espectro debe caer a cero."""
        with self._lock:
            self._filled = 0
            self._head = 0
            self.ready = False
            self._agc_db = 0.0

    def close(self) -> None:
        self._closed = True
        self.silence()

    # -- lectura -----------------------------------------------------------
    def _window_at(self, position: float, size: int = FFT_SIZE) -> tuple | None:
        with self._lock:
            if self._filled < size:
                return None
            back = size + int(self.lag * SAMPLE_RATE)
            if back > self._filled:
                back = self._filled
            start = (self._head - back) % self._size
            end = start + size
            if end <= self._size:
                raw = bytes(self._buf[start * 2:end * 2])
            else:
                split = self._size - start
                raw = bytes(self._buf[start * 2:]) + bytes(self._buf[:(size - split) * 2])
        try:
            if size == FFT_SIZE:
                return _UNPACK(raw, 0)
            return struct.unpack_from(f"<{size}h", raw, 0)
        except (ValueError, struct.error):
            return None
