"""PCM -> MP3 (320 kbit/s) -> endloser HTTP-Stream.

Ein Takt-Thread erzeugt exakt Echtzeit-Audio: fehlt Material (Programm pausiert),
wird Stille eingefuegt, damit Sonos den Stream nicht abbricht.
"""
import queue
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import lameenc

from .capture import BYTES_PER_FRAME, CHANNELS, SAMPLE_RATE

TICK = 0.02                      # 20 ms
PRIME = int(0.10 * SAMPLE_RATE)  # Jitterpuffer 100 ms
MAX_BUF = int(0.40 * SAMPLE_RATE)
STREAM_PATH = "/stream.mp3"


class Streamer:
    def __init__(self, port=8090, bitrate=320):
        self.port = port
        self.bitrate = bitrate
        self._buf = bytearray()
        self._lock = threading.Lock()
        self._clients = set()
        self._clients_lock = threading.Lock()
        self._stop = threading.Event()
        self._primed = False
        self.level = 0.0  # einfacher Pegel fuer die GUI (0..1)
        self._server = None
        self._inject = None  # (pcm, Event, [zeitpunkt]) fuer die Kalibrierung

    def inject(self, pcm: bytes, timeout=2.0):
        """Mischt `pcm` in den Stream (nur Sonos, nicht PC). Gibt den Zeitpunkt
        (perf_counter) zurueck, zu dem der Anfang gesendet wurde, plus die damalige
        Puffer-Verzoegerung gegenueber der Aufnahme in Sekunden."""
        ev, out = threading.Event(), []
        self._inject = (pcm, ev, out)
        if not ev.wait(timeout):
            raise TimeoutError("Stream laeuft nicht")
        return out[0], out[1]

    def _mix_inject(self, pcm: bytes) -> bytes:
        inj = self._inject
        if not inj:
            return pcm
        data, ev, out = inj
        n = min(len(pcm), len(data))
        if not out:
            with self._lock:
                buffered = len(self._buf) / BYTES_PER_FRAME / SAMPLE_RATE
            out += [time.perf_counter(), buffered]
        mv_a, mv_b = memoryview(pcm).cast("h"), memoryview(data[:n]).cast("h")
        mixed = bytearray(pcm)
        mm = memoryview(mixed).cast("h")
        for i in range(n // 2):
            mm[i] = max(-32768, min(32767, mv_a[i] + mv_b[i]))
        rest = data[n:]
        self._inject = (rest, ev, out) if rest else None
        if not rest:
            ev.set()
        return bytes(mixed)

    # --- Eingang -------------------------------------------------------------
    def feed(self, pcm: bytes):
        with self._lock:
            self._buf += pcm
            if len(self._buf) > MAX_BUF * BYTES_PER_FRAME:
                del self._buf[: len(self._buf) - PRIME * BYTES_PER_FRAME]

    def _take(self, frames):
        need = frames * BYTES_PER_FRAME
        with self._lock:
            avail = len(self._buf)
            if not self._primed:
                if avail >= PRIME * BYTES_PER_FRAME:
                    self._primed = True
                else:
                    return bytes(need)
            out = bytes(self._buf[:need])
            del self._buf[:need]
            if len(out) < need:  # Unterlauf -> Stille, neu vorpuffern
                self._primed = False
                out += bytes(need - len(out))
            return out

    # --- Takt + Encoder ------------------------------------------------------
    def _clock(self):
        enc = lameenc.Encoder()
        enc.set_bit_rate(self.bitrate)
        enc.set_in_sample_rate(SAMPLE_RATE)
        enc.set_channels(CHANNELS)
        enc.set_quality(2)
        start = time.perf_counter()
        produced = 0
        while not self._stop.is_set():
            due = int((time.perf_counter() - start) * SAMPLE_RATE) - produced
            if due > 0:
                pcm = self._mix_inject(self._take(due))
                produced += due
                self._update_level(pcm)
                mp3 = enc.encode(pcm)
                if mp3:
                    self._broadcast(bytes(mp3))
            time.sleep(TICK)

    def _update_level(self, pcm):
        if len(pcm) < 400:
            return
        # grobe Spitze ueber eine Stichprobe
        mv = memoryview(pcm).cast("h")
        peak = max(abs(x) for x in mv[::64]) / 32768
        self.level = max(peak, self.level * 0.85)

    def _broadcast(self, data):
        with self._clients_lock:
            for q in list(self._clients):
                try:
                    q.put_nowait(data)
                except queue.Full:
                    pass

    # --- HTTP ----------------------------------------------------------------
    def _make_handler(self):
        streamer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *a):
                pass

            def _headers(self):
                self.send_response(200)
                self.send_header("Content-Type", "audio/mpeg")
                self.send_header("Cache-Control", "no-cache, no-store")
                self.send_header("icy-name", "PC")
                self.send_header("Connection", "close")
                self.end_headers()

            def do_HEAD(self):
                if self.path.split("?")[0] != STREAM_PATH:
                    self.send_error(404)
                    return
                self._headers()

            def do_GET(self):
                if self.path.split("?")[0] != STREAM_PATH:
                    self.send_error(404)
                    return
                self._headers()
                q = queue.Queue(maxsize=500)
                with streamer._clients_lock:
                    streamer._clients.add(q)
                try:
                    while not streamer._stop.is_set():
                        try:
                            data = q.get(timeout=1)
                        except queue.Empty:
                            continue
                        self.wfile.write(data)
                except (ConnectionError, OSError):
                    pass
                finally:
                    with streamer._clients_lock:
                        streamer._clients.discard(q)

        return Handler

    @property
    def client_count(self):
        with self._clients_lock:
            return len(self._clients)

    def start(self):
        self._stop.clear()
        self._server = ThreadingHTTPServer(("0.0.0.0", self.port), self._make_handler())
        self._server.daemon_threads = True
        threading.Thread(target=self._server.serve_forever, name="http", daemon=True).start()
        threading.Thread(target=self._clock, name="clock", daemon=True).start()

    def stop(self):
        self._stop.set()
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._server = None


def local_ip_for(target_ip=None):
    """Lokale IP, ueber die target_ip erreicht wird (richtige Netzwerkkarte bei VPN/mehreren NICs)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((target_ip or "192.168.0.1", 1400))
        return s.getsockname()[0]
    finally:
        s.close()


def local_ips():
    ips = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    ips.discard("127.0.0.1")
    return sorted(ips)
