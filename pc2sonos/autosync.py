"""Fortlaufende Synchronisation PC <-> Sonos ueber ein leises Rauschwasserzeichen.

In Sonos- und PC-Zweig wird je ein eigenes, zufaelliges Rauschsignal (2-8 kHz)
gemischt, ca. 30 dB leiser als die Musik (Spread Spectrum). Das Mikrofon hoert
beides (im WASAPI-Raw-Modus, damit keine Rauschunterdrueckung es wegfiltert).
Pro Zweig wird ueber 30 s in 3-s-Teilfenstern per GCC-PHAT korreliert und
aufaddiert. Der Abstand der beiden Treffer = Fehler des PC-Versatzes; Mikrofon-
und Treiberlatenzen fallen heraus, weil beide Zweige gleich betroffen sind.
Offline-Test (Fake-Sonos +300 ms): gemessen +304 ms, z-Wert 18 bei 30 s.
"""
from __future__ import annotations

import collections
import threading
import time

import numpy as np
import sounddevice as sd

SR = 44100
BAND = (2000, 8000)
LEVEL_DB = -30.0       # relativ zum aktuellen Musikpegel
FLOOR = 10 ** (-75 / 20)  # Mindestpegel (bei Stille praktisch nichts)
HISTORY_S = 60.0


def _bandpass_fir(taps=127):
    n = np.arange(taps) - (taps - 1) / 2
    def lp(fc):
        return 2 * fc / SR * np.sinc(2 * fc / SR * n)
    h = (lp(BAND[1]) - lp(BAND[0])) * np.hamming(taps)
    return h.astype(np.float64)


class Watermark:
    """Erzeugt band-begrenztes Rauschen und merkt sich, was gesendet wurde
    (indiziert ueber den Aufnahme-Sampleindex)."""

    def __init__(self, seed):
        self.rng = np.random.default_rng(seed)
        self.fir = _bandpass_fir()
        self.tail = np.zeros(len(self.fir) - 1)
        self.hist = np.zeros(int(HISTORY_S * SR), dtype=np.float32)
        self.end = 0  # Aufnahme-Index hinter dem letzten Sample
        self.lock = threading.Lock()
        self.gain = 1.0 / np.sqrt(np.sum(self.fir ** 2))  # Rauschen ~ Einheits-RMS

    def make(self, n, music_rms):
        x = self.rng.standard_normal(n)
        y = np.convolve(np.concatenate([self.tail, x]), self.fir, "valid") * self.gain
        self.tail = np.concatenate([self.tail, x])[-(len(self.fir) - 1):]
        y *= max(FLOOR, music_rms * 10 ** (LEVEL_DB / 20))
        with self.lock:
            size = len(self.hist)
            yy = y[-size:]
            pos = (self.end + n - len(yy)) % size  # Ringpuffer: Index i liegt bei i % size
            first = min(len(yy), size - pos)
            self.hist[pos:pos + first] = yy[:first]
            self.hist[: len(yy) - first] = yy[first:]
            self.end += n
        return y

    def segment(self, start, stop):
        """Gesendetes Wasserzeichen fuer Aufnahme-Index [start, stop) oder None."""
        with self.lock:
            size = len(self.hist)
            if start < self.end - size or stop > self.end or stop <= start:
                return None
            idx = np.arange(start, stop) % size
            return self.hist[idx].astype(np.float64)


SUB_S = 3.0  # Teilfenster: kurz genug, dass Taktabweichungen nicht stoeren


def find_lag(mic, n0, wm, lo_s, hi_s):
    """Verzoegerung (Samples) vom Aufnahme-Index zum Mikrofon fuer ein Wasserzeichen.

    Das Mikrofonfenster wird in 3-s-Teilfenster zerlegt; jedes wird per GCC-PHAT
    (2-8 kHz) mit dem gesendeten Wasserzeichen korreliert, die geglaetteten
    Huellkurven werden normiert aufaddiert. So stoeren weder Musik noch Raumhall
    noch minimal unterschiedliche Geraetetakte. Rueckgabe (lag, z-Wert) oder None.
    """
    lo, hi = int(lo_s * SR), int(hi_s * SR)
    L = int(SUB_S * SR)
    K = len(mic) // L
    if K < 1:
        return None
    ker = np.ones(44) / 44  # 1 ms glaetten
    acc = None
    for k in range(K):
        m = mic[k * L:(k + 1) * L]
        s = wm.segment(n0 + k * L - hi, n0 + k * L + L - lo)
        if s is None:
            continue
        n = 1 << int(np.ceil(np.log2(len(s) + len(m))))
        R = np.fft.rfft(s, n) * np.conj(np.fft.rfft(m, n))
        R /= np.abs(R) + 1e-9
        f = np.fft.rfftfreq(n, 1 / SR)
        R[(f < BAND[0]) | (f > BAND[1])] = 0
        c = np.fft.irfft(R, n)[: len(s) - len(m) + 1]
        e = np.convolve(np.abs(c), ker, "same")
        e = (e - np.median(e)) / (np.std(e) + 1e-12)
        acc = e if acc is None else acc + e
        time.sleep(0.01)  # Audio-Threads kurz rechnen lassen
    if acc is None:
        return None
    u = int(acc.argmax())
    z = float((acc[u] - acc.mean()) / (acc.std() + 1e-12))
    return hi - u, z


class AutoSync:
    """Hoert per Mikrofon mit und fuehrt den PC-Versatz laufend nach."""

    def __init__(self, splitter, local, on_update=None, window_s=30.0, every_s=10.0,
                 min_quality=6.5, mic_device=None):
        self.splitter = splitter
        self.local = local
        self.on_update = on_update or (lambda *a: None)
        self.window = int(window_s * SR)
        self.every = every_s
        self.min_quality = min_quality
        self.mic_device = mic_device
        self._mic = collections.deque()
        self._mic_len = 0
        self._marks = collections.deque()  # (mic_total_samples, capture_index)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._recent = collections.deque(maxlen=3)
        self._hold_until = 0.0
        self.last = None  # (fehler_ms, guete_sonos, guete_pc)

    def _cb(self, indata, frames, ti, status):
        self._push(indata[:, 0].copy())

    def _push(self, a):
        frames = len(a)
        with self._lock:
            self._mic.append(a)
            self._mic_len += frames  # Samples im Puffer
            self._marks.append((0, self.splitter.index))
            keep = self.window * 2
            while len(self._mic) > 1 and self._mic_len - len(self._mic[0]) >= keep:
                self._mic_len -= len(self._mic.popleft())
            while len(self._marks) > 200:
                self._marks.popleft()

    def start(self):
        self._raw = None
        import sys
        if sys.platform == "win32" and self.mic_device is None:
            # Raw-Modus: umgeht Windows-Rauschunterdrueckung (Voice Clarity usw.),
            # die das Wasserzeichen sonst als Rauschen herausfiltert.
            try:
                from .mic_raw import RawMic
                self._raw = RawMic(self._push)
                self._raw.start()
            except Exception:  # noqa: BLE001
                self._raw = None
        if self._raw:
            self._thread = threading.Thread(target=self._loop, name="autosync", daemon=True)
            self._thread.start()
            return
        extra = None
        try:
            d = sd.query_devices(self.mic_device if self.mic_device is not None else sd.default.device[0])
            if sd.query_hostapis(d["hostapi"])["name"] == "Windows WASAPI":
                extra = sd.WasapiSettings(auto_convert=True)
        except Exception:  # noqa: BLE001
            pass
        self._stream = sd.InputStream(samplerate=SR, channels=1, dtype="float32", device=self.mic_device,
                                      callback=self._cb, extra_settings=extra)
        self._stream.start()
        self._thread = threading.Thread(target=self._loop, name="autosync", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if getattr(self, "_raw", None):
            self._raw.stop()
            return
        try:
            self._stream.stop()
            self._stream.close()
        except Exception:  # noqa: BLE001
            pass

    def _snapshot(self):
        with self._lock:
            if not self._mic or not self._marks:
                return None
            data = np.concatenate(list(self._mic))
            cap_idx = self._marks[-1][1]
        if len(data) < self.window:
            return None
        mic = data[-self.window:].astype(np.float64)
        # Aufnahme-Index, der "jetzt" galt, minus Fensterlaenge = Index am Fensteranfang
        n0 = cap_idx - self.window
        return mic, n0

    def measure_once(self):
        snap = self._snapshot()
        if not snap:
            return None
        mic, n0 = snap
        d_local = self.local.delay_ms / 1000
        r_pc = find_lag(mic, n0, self.splitter.wm_local, max(0.0, d_local - 0.4), d_local + 1.5)
        # Sonos-Verzoegerung liegt in der Naehe des aktuellen PC-Versatzes
        r_so = find_lag(mic, n0, self.splitter.wm_sonos, max(0.2, d_local - 3.0), d_local + 4.0)
        if not r_pc or not r_so:
            return None
        (lag_pc, q_pc), (lag_so, q_so) = r_pc, r_so
        err_ms = (lag_so - lag_pc) / SR * 1000
        return err_ms, q_so, q_pc

    def _loop(self):
        while not self._stop.wait(self.every):
            try:
                r = self.measure_once()
            except Exception as e:  # noqa: BLE001
                self.on_update(None, f"Auto-Sync Fehler: {e}")
                continue
            if r is None:
                continue
            if time.monotonic() < self._hold_until:
                continue  # Fenster enthaelt noch Zeit vor/waehrend der letzten Korrektur
            err, q_so, q_pc = r
            self.last = (round(err), round(q_so, 1), round(q_pc, 1))
            if q_so < self.min_quality or q_pc < self.min_quality:
                self.on_update(None, f"Auto-Sync: misst … (Signal schwach, Güte {min(q_so, q_pc):.0f})")
                continue
            self._recent.append(err)
            if len(self._recent) < 2:
                self.on_update(None, f"Auto-Sync: misst … ({err:+.0f} ms, warte auf Bestätigung)")
                continue
            # zwei uebereinstimmende Messungen (+-30 ms) noetig; Zufallstreffer stimmen nie ueberein
            vals = sorted(self._recent)
            pairs = [(a, b) for i, a in enumerate(vals) for b in vals[i + 1:] if abs(a - b) <= 30]
            if not pairs:
                self.on_update(None, "Auto-Sync: misst … (Werte noch uneinig)")
                continue
            a, b = pairs[-1]
            med = (a + b) / 2
            if abs(med) >= 8:
                new = int(round(self.local.delay_ms + med))
                smooth = abs(med) < 60
                self.local.set_delay(new, smooth=smooth)
                self._recent.clear()
                # Gleiten mit ~0,5 %: |med| ms dauern ~ |med|/5 s; danach ein volles Fenster abwarten
                glide = abs(med) / 5 if smooth else 0
                self._hold_until = time.monotonic() + glide + self.window / SR
                self.on_update(new, f"Auto-Sync: {med:+.0f} ms korrigiert, Versatz jetzt {new} ms")
            else:
                self.on_update(None, f"Auto-Sync: synchron (Abweichung {med:+.0f} ms)")
