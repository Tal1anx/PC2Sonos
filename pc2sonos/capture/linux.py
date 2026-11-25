"""Linux-Aufnahme ueber PulseAudio/PipeWire (pactl + parec).

System-Modus: Monitor der Standardausgabe.
App-Modus:    eigener Null-Sink 'pc2sonos', der Sink-Input des Programms wird dorthin
              verschoben und dessen Monitor aufgenommen (Programm ist dann lokal stumm).
Noch nicht auf echter Hardware getestet.
"""
import json
import subprocess
import threading

SINK = "pc2sonos"


def _pactl(*args):
    return subprocess.run(["pactl", *args], capture_output=True, text=True, check=True).stdout


def _sink_inputs():
    try:
        return json.loads(_pactl("-f", "json", "list", "sink-inputs"))
    except Exception:  # noqa: BLE001
        return []


def list_audio_apps():
    apps = {}
    for si in _sink_inputs():
        props = si.get("properties", {})
        name = props.get("application.process.binary") or props.get("application.name") or "?"
        pid = int(props.get("application.process.id", "0") or 0)
        apps.setdefault(name.lower(), (name, pid))
    return sorted(apps.values(), key=lambda a: a[0].lower())


def find_app_pid(name):
    for n, pid in list_audio_apps():
        if n.lower() == name.lower():
            return pid
    return None


class LinuxCapture:
    def __init__(self, callback, pid=None):
        self.callback = callback
        self.pid = pid
        self._proc = None
        self._thread = None
        self._module = None

    def start(self):
        if self.pid is None:
            source = "@DEFAULT_MONITOR@"
        else:
            self._module = _pactl("load-module", "module-null-sink", f"sink_name={SINK}").strip()
            for si in _sink_inputs():
                if int(si.get("properties", {}).get("application.process.id", "0") or 0) == self.pid:
                    _pactl("move-sink-input", str(si["index"]), SINK)
            source = f"{SINK}.monitor"
        self._proc = subprocess.Popen(
            ["parec", f"--device={source}", "--format=s16le", "--rate=44100", "--channels=2",
             "--latency-msec=20"],
            stdout=subprocess.PIPE)
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        while True:
            chunk = self._proc.stdout.read(3528)  # ~20 ms
            if not chunk:
                break
            self.callback(chunk)

    def stop(self):
        if self._proc:
            self._proc.terminate()
        if self._module:
            try:
                _pactl("unload-module", self._module)
            except Exception:  # noqa: BLE001
                pass
