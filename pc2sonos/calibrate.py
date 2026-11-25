"""Synchronisation PC <-> Sonos per Mikrofon.

An derselben Stelle des aufgenommenen Tons werden zwei Piepmuster eingemischt:
hoch (2,5 kHz) nur in den Sonos-Stream, tief (1,2 kHz) nur in die PC-Ausgabe.
Das Mikrofon hoert beide; gemessen wird nur ihr ABSTAND. Damit fallen alle
Latenzen von Mikrofon, Treibern und Ausgabegeraeten heraus.

    neuer Versatz = aktueller Versatz + (Ankunft Sonos - Ankunft PC)
"""
import threading
import time

import numpy as np
import sounddevice as sd

SR = 44100
F_SONOS = 2500
F_LOCAL = 1200
BEEP = 0.04
OFFSETS = (0.0, 0.55, 1.45)  # unregelmaessig -> eindeutiges Muster
LISTEN = 12.0


def _pattern(freq):
    n = int(BEEP * SR)
    t = np.arange(n) / SR
    beep = np.sin(2 * np.pi * freq * t) * np.hanning(n)
    pat = np.zeros(int((OFFSETS[-1] + BEEP) * SR))
    for o in OFFSETS:
        s = int(o * SR)
        pat[s:s + n] += beep
    return pat


def _pcm(pat, volume):
    return (np.repeat(pat[:, None], 2, axis=1) * volume * 32767).astype("<i2").tobytes()


def _band(x, freq):
    X = np.fft.rfft(x)
    f = np.fft.rfftfreq(len(x), 1 / SR)
    X[(f < freq - 250) | (f > freq + 250)] = 0
    return np.fft.irfft(X, len(x))


def _find(sig, freq, window=None):
    tpl = _band(_pattern(freq), freq)
    s = _band(sig, freq)
    n = 1 << int(np.ceil(np.log2(len(s) + len(tpl))))
    corr = np.abs(np.fft.irfft(np.fft.rfft(s, n) * np.conj(np.fft.rfft(tpl, n)), n)[: len(s)])
    noise = np.median(corr) + 1e-12
    if window:
        a, b = (max(0, int(w * SR)) for w in window)
        masked = np.zeros_like(corr)
        masked[a:b] = corr[a:b]
        corr = masked
    peak = int(corr.argmax())
    return peak, float(corr[peak] / noise)


class Splitter:
    """Verteilt den aufgenommenen Ton auf Sonos-Stream und PC-Ausgabe und kann
    an derselben Stelle in beide Zweige unterschiedliche Signale einmischen."""

    def __init__(self, to_stream, to_local=None):
        self.to_stream = to_stream
        self.to_local = to_local
        self._pending = None  # [bytearray sonos, bytearray local]
        self._lock = threading.Lock()

    @staticmethod
    def _mix(pcm, extra):
        n = min(len(pcm), len(extra))
        if not n:
            return pcm
        a = np.frombuffer(pcm, dtype="<i2").astype(np.int32)
        b = np.frombuffer(bytes(extra[:n]), dtype="<i2").astype(np.int32)
        a[: n // 2] += b
        del extra[:n]
        return np.clip(a, -32768, 32767).astype("<i2").tobytes()

    index = 0          # Aufnahme-Sampleindex (Frames) hinter dem letzten feed
    wm_sonos = None    # autosync.Watermark, wenn fortlaufende Synchronisation aktiv
    wm_local = None

    def enable_watermark(self, on=True):
        from .autosync import Watermark
        with self._lock:
            if on:
                self.wm_sonos, self.wm_local = Watermark(1), Watermark(2)
                for w in (self.wm_sonos, self.wm_local):
                    w.end = self.index
            else:
                self.wm_sonos = self.wm_local = None

    def _add_wm(self, pcm, wm, music_rms):
        a = np.frombuffer(pcm, dtype="<i2").astype(np.float64).reshape(-1, 2)
        w = wm.make(len(a), music_rms) * 32767
        a += w[:, None]
        return np.clip(a, -32768, 32767).astype("<i2").tobytes()

    def feed(self, pcm):
        with self._lock:
            p = self._pending
            if p:
                s, l = self._mix(pcm, p[0]), self._mix(pcm, p[1])
                if not p[0] and not p[1]:
                    self._pending = None
            else:
                s = l = pcm
            if self.wm_sonos is not None:
                a = np.frombuffer(pcm, dtype="<i2").astype(np.float64)
                rms = float(np.sqrt(np.mean((a / 32768) ** 2))) if len(a) else 0.0
                s = self._add_wm(s, self.wm_sonos, rms)
                l = self._add_wm(l, self.wm_local, rms)
            self.index += len(pcm) // 4
        self.to_stream(s)
        if self.to_local:
            self.to_local(l)

    def inject(self, sonos_pcm, local_pcm):
        with self._lock:
            self._pending = [bytearray(sonos_pcm), bytearray(local_pcm)]


def measure_offset(splitter, volume=0.3, mic_device=None, local_delay_ms=0):
    """Misst, wie viel spaeter die Sonos als die PC-Ausgabe zu hoeren sind.
    Rueckgabe: (differenz_ms, guete_sonos, guete_pc). Guete > ~8 ist verlaesslich."""
    chunks = []
    lock = threading.Lock()

    def cb(indata, frames, ti, status):
        with lock:
            chunks.append(indata[:, 0].copy())

    extra = None
    try:
        d = sd.query_devices(mic_device if mic_device is not None else sd.default.device[0])
        if sd.query_hostapis(d["hostapi"])["name"] == "Windows WASAPI":
            extra = sd.WasapiSettings(auto_convert=True)
    except Exception:  # noqa: BLE001
        pass
    with sd.InputStream(samplerate=SR, channels=1, dtype="float32", device=mic_device, callback=cb,
                        extra_settings=extra):
        time.sleep(0.3)
        splitter.inject(_pcm(_pattern(F_SONOS), volume), _pcm(_pattern(F_LOCAL), volume))
        time.sleep(LISTEN)
    with lock:
        if not chunks:
            raise RuntimeError("Mikrofon liefert nichts")
        mic = np.concatenate(chunks)
    # PC-Ton kann nur kurz nach Ablauf des aktuellen Versatzes kommen (+ max. ~1 s Geraetelatenz)
    t_pc = 0.3 + local_delay_ms / 1000
    p_l, q_l = _find(mic, F_LOCAL, window=(t_pc - 0.2, t_pc + 1.2))
    p_s, q_s = _find(mic, F_SONOS, window=(0.3, len(mic) / SR))
    return int(round((p_s - p_l) / SR * 1000)), round(q_s, 1), round(q_l, 1)


def calibrate(splitter, local_delay_ms, mic_device=None, tries=3, min_quality=8.0, progress=None):
    """Mehrfach messen; gibt den neuen PC-Versatz in ms zurueck (oder wirft RuntimeError)."""
    results = []
    for i in range(tries):
        if progress:
            progress(f"Messung {i + 1}/{tries} …")
        d, q_s, q_l = measure_offset(splitter, mic_device=mic_device, local_delay_ms=local_delay_ms)
        if q_s >= min_quality and q_l >= min_quality:
            results.append(d)
            close = [r for r in results if abs(r - d) <= 150]
            if len(close) >= 2:
                return max(0, local_delay_ms + int(np.median(close)))
    raise RuntimeError(
        "Keine eindeutige Messung. Mikrofon näher an Lautsprecher/Sonos, oder Lautstärke erhöhen."
        + (f" (Werte: {results})" if results else ""))
