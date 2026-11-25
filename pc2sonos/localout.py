"""Verzoegerte Wiedergabe am PC, damit PC-Lautsprecher und Sonos synchron laufen.

Der aufgenommene Ton wird gepuffert und mit `delay_ms` Versatz auf einem lokalen
Ausgabegeraet abgespielt. Die Latenz des Ausgabegeraets wird abgezogen.

Grosse Versatzaenderungen (von Hand) springen; kleine Korrekturen (Auto-Sync,
Uhrdrift) werden unhoerbar nachgefuehrt, indem pro Block hoechstens ~0,5 %
der Samples weggelassen bzw. verdoppelt werden.
"""
import threading

import numpy as np
import sounddevice as sd

SR = 44100
BPF = 4  # 16 bit stereo
JUMP = int(0.30 * SR)        # ab hier springen statt gleiten (Frames)
DEADBAND = int(0.015 * SR)   # Pufferschwankung bis 15 ms ignorieren
SETTLE = int(0.004 * SR)     # beim Nachfuehren bis auf 4 ms heranfuehren


def wasapi_extra(device):
    """WASAPI nimmt im Shared-Mode nur die Geraeterate (oft 48 kHz) an – ausser mit
    auto_convert. Fuer andere Host-APIs None."""
    try:
        if device is None:
            device = sd.default.device[1]
        d = sd.query_devices(device)
        if sd.query_hostapis(d["hostapi"])["name"] == "Windows WASAPI":
            return sd.WasapiSettings(auto_convert=True)
    except Exception:  # noqa: BLE001
        pass
    return None


def output_devices():
    """Namen der WASAPI-Ausgabegeraete und Index des Standardgeraets."""
    try:
        api = next(i for i, a in enumerate(sd.query_hostapis()) if a["name"] == "Windows WASAPI")
    except StopIteration:
        api = sd.default.hostapi
    devs = [(i, d["name"]) for i, d in enumerate(sd.query_devices())
            if d["hostapi"] == api and d["max_output_channels"] >= 2]
    default = sd.query_hostapis(api)["default_output_device"]
    return devs, default


class LocalOutput:
    def __init__(self, device=None, delay_ms=5000):
        self.device = device
        self._buf = bytearray()
        self._lock = threading.Lock()
        self._delay_ms = delay_ms
        self._stream = None
        self._correcting = False
        self._jump = False

    @property
    def delay_ms(self):
        return self._delay_ms

    def set_delay(self, ms, smooth=False):
        """smooth=True: unhoerbar nachfuehren (fuer kleine Korrekturen)."""
        self._delay_ms = max(0, int(ms))
        if smooth:
            self._correcting = True
        else:
            self._jump = True

    def _target_frames(self):
        out_lat = self._stream.latency if self._stream else 0.0
        return max(0, int((self._delay_ms / 1000 - out_lat) * SR))

    def start(self):
        self._stream = sd.RawOutputStream(samplerate=SR, channels=2, dtype="int16",
                                          # "high" (~100 ms Puffer): robust gegen kurze Rechenlast
                                          # (Auto-Sync-FFTs); die Latenz wird vom Versatz abgezogen
                                          device=self.device, callback=self._cb, latency="high",
                                          extra_settings=wasapi_extra(self.device))
        with self._lock:
            self._buf = bytearray(self._target_frames() * BPF)
        self._stream.start()

    def stop(self):
        if self._stream:
            self._stream.stop()
            self._stream.close()
            self._stream = None

    def feed(self, pcm: bytes):
        with self._lock:
            self._buf += pcm

    def _take(self, n):
        chunk = bytes(self._buf[: n * BPF])
        del self._buf[: n * BPF]
        if len(chunk) < n * BPF:
            chunk += bytes(n * BPF - len(chunk))
        return chunk

    underflows = 0

    def _cb(self, outdata, frames, time_info, status):
        if status.output_underflow:
            self.underflows += 1
        with self._lock:
            target = self._target_frames()
            level = len(self._buf) // BPF
            err = level - frames - target  # >0: zu viel gepuffert (PC zu spaet)

            if self._jump:  # Aenderung von Hand: sofort umsetzen
                self._jump = False
                if err > 0:
                    del self._buf[: err * BPF]
                elif err < 0:
                    self._buf[0:0] = bytes(-err * BPF)
                self._correcting = False
                out = self._take(frames)
            elif err > JUMP:
                del self._buf[: (level - frames - target) * BPF]
                out = self._take(frames)
            elif err < -JUMP:
                self._buf[0:0] = bytes(-err * BPF)  # Luecke auffuellen
                out = self._take(frames)
            else:
                if abs(err) > DEADBAND:
                    self._correcting = True
                elif abs(err) <= SETTLE:
                    self._correcting = False
                k = 0
                if self._correcting:
                    k = min(abs(err), max(1, frames // 200))
                if k and err > 0:
                    # k Frames mehr lesen und gleichmaessig verteilt weglassen
                    a = np.frombuffer(self._take(frames + k), dtype="<i2").reshape(-1, 2)
                    idx = np.linspace(0, len(a) - 1, k + 2, dtype=int)[1:-1]
                    out = np.delete(a, idx, axis=0).tobytes()
                elif k and err < 0:
                    # k Frames weniger lesen und gleichmaessig verteilt verdoppeln
                    a = np.frombuffer(self._take(frames - k), dtype="<i2").reshape(-1, 2)
                    idx = np.linspace(0, len(a) - 1, k + 2, dtype=int)[1:-1]
                    out = np.insert(a, idx, a[idx], axis=0).tobytes()
                else:
                    out = self._take(frames)
        outdata[:] = out
