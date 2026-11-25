"""macOS-Aufnahme – geplant: Core Audio Process Taps (macOS 14.4+) ueber pyobjc.

Platzhalter, damit Sonos-Steuerung und GUI auf dem Mac schon starten.
"""


def list_audio_apps():
    return []


def find_app_pid(name):
    return None


class MacCapture:
    def __init__(self, callback, pid=None):
        self.callback = callback
        self.pid = pid

    def start(self):
        raise NotImplementedError("macOS-Aufnahme ist noch nicht umgesetzt (Core Audio Process Tap folgt).")

    def stop(self):
        pass
