"""Plattform-Auswahl fuer die Audio-Aufnahme.

Jedes Backend liefert 44.1 kHz / 16 bit / Stereo PCM (s16le) an callback(bytes)
und bietet:
    Capture(callback, pid=None)   pid=None -> ganzer PC-Sound
    list_audio_apps() -> [(name, pid)]
    find_app_pid(name) -> pid | None
"""
import sys

SAMPLE_RATE = 44100
CHANNELS = 2
BYTES_PER_FRAME = 4

if sys.platform == "win32":
    from .windows import WindowsCapture as Capture, find_app_pid, list_audio_apps
elif sys.platform.startswith("linux"):
    from .linux import LinuxCapture as Capture, find_app_pid, list_audio_apps
elif sys.platform == "darwin":
    from .macos import MacCapture as Capture, find_app_pid, list_audio_apps
else:
    raise ImportError(f"Plattform {sys.platform} wird nicht unterstuetzt")

__all__ = ["Capture", "list_audio_apps", "find_app_pid", "SAMPLE_RATE", "CHANNELS", "BYTES_PER_FRAME"]
