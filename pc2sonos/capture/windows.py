"""Windows-Aufnahme per WASAPI Process Loopback (Windows 10 2004+ / 11).

- App-Modus:    nur ein Prozess(baum) wird aufgenommen (INCLUDE_TARGET_PROCESS_TREE).
- System-Modus: alles ausser unserem eigenen Prozess (EXCLUDE_TARGET_PROCESS_TREE),
                also praktisch der komplette PC-Sound.
Kein virtuelles Kabel, kein Treiber, keine Umstellung des Standardgeraets.
"""
import ctypes
import os
import threading
from ctypes import POINTER, Structure, c_byte, c_int, c_longlong, c_uint32, c_uint64, c_ushort, c_void_p, c_wchar_p
from ctypes.wintypes import DWORD

import comtypes
from comtypes import COMMETHOD, COMObject, GUID, HRESULT, IUnknown

SAMPLE_RATE = 44100
CHANNELS = 2
BYTES_PER_FRAME = 4  # 16 bit stereo

VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK = "VAD\\Process_Loopback"
AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK = 1
PROCESS_LOOPBACK_MODE_INCLUDE = 0
PROCESS_LOOPBACK_MODE_EXCLUDE = 1

AUDCLNT_STREAMFLAGS_LOOPBACK = 0x00020000
AUDCLNT_STREAMFLAGS_EVENTCALLBACK = 0x00040000
AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY = 0x08000000
AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM = 0x80000000
AUDCLNT_BUFFERFLAGS_SILENT = 0x2
VT_BLOB = 65


class WAVEFORMATEX(Structure):
    _pack_ = 1
    _fields_ = [
        ("wFormatTag", c_ushort),
        ("nChannels", c_ushort),
        ("nSamplesPerSec", DWORD),
        ("nAvgBytesPerSec", DWORD),
        ("nBlockAlign", c_ushort),
        ("wBitsPerSample", c_ushort),
        ("cbSize", c_ushort),
    ]


class AUDIOCLIENT_ACTIVATION_PARAMS(Structure):
    _fields_ = [
        ("ActivationType", c_int),
        ("TargetProcessId", DWORD),
        ("ProcessLoopbackMode", c_int),
    ]


class PROPVARIANT_BLOB(Structure):
    _fields_ = [
        ("vt", c_ushort),
        ("r1", c_ushort),
        ("r2", c_ushort),
        ("r3", c_ushort),
        ("cbSize", c_uint32),
        ("pBlobData", c_void_p),
    ]


class IAudioCaptureClient(IUnknown):
    _iid_ = GUID("{C8ADBD64-E71E-48a0-A4DE-185C395CD317}")
    _methods_ = [
        COMMETHOD([], HRESULT, "GetBuffer",
                  (["out"], POINTER(POINTER(c_byte)), "data"),
                  (["out"], POINTER(c_uint32), "frames"),
                  (["out"], POINTER(DWORD), "flags"),
                  (["out"], POINTER(c_uint64), "devpos"),
                  (["out"], POINTER(c_uint64), "qpcpos")),
        COMMETHOD([], HRESULT, "ReleaseBuffer", (["in"], c_uint32, "frames")),
        COMMETHOD([], HRESULT, "GetNextPacketSize", (["out"], POINTER(c_uint32), "frames")),
    ]


class IAudioClient(IUnknown):
    _iid_ = GUID("{1CB9AD4C-DBFA-4c32-B178-C2F568A703B2}")
    _methods_ = [
        COMMETHOD([], HRESULT, "Initialize",
                  (["in"], c_int, "ShareMode"),
                  (["in"], DWORD, "StreamFlags"),
                  (["in"], c_longlong, "hnsBufferDuration"),
                  (["in"], c_longlong, "hnsPeriodicity"),
                  (["in"], POINTER(WAVEFORMATEX), "pFormat"),
                  (["in"], POINTER(GUID), "AudioSessionGuid")),
        COMMETHOD([], HRESULT, "GetBufferSize", (["out"], POINTER(c_uint32), "n")),
        COMMETHOD([], HRESULT, "GetStreamLatency", (["out"], POINTER(c_longlong), "l")),
        COMMETHOD([], HRESULT, "GetCurrentPadding", (["out"], POINTER(c_uint32), "p")),
        COMMETHOD([], HRESULT, "IsFormatSupported",
                  (["in"], c_int, "m"), (["in"], POINTER(WAVEFORMATEX), "f"),
                  (["out"], POINTER(POINTER(WAVEFORMATEX)), "c")),
        COMMETHOD([], HRESULT, "GetMixFormat", (["out"], POINTER(POINTER(WAVEFORMATEX)), "f")),
        COMMETHOD([], HRESULT, "GetDevicePeriod",
                  (["out"], POINTER(c_longlong), "d"), (["out"], POINTER(c_longlong), "m")),
        COMMETHOD([], HRESULT, "Start"),
        COMMETHOD([], HRESULT, "Stop"),
        COMMETHOD([], HRESULT, "Reset"),
        COMMETHOD([], HRESULT, "SetEventHandle", (["in"], c_void_p, "h")),
        COMMETHOD([], HRESULT, "GetService",
                  (["in"], POINTER(GUID), "riid"), (["out"], POINTER(POINTER(IUnknown)), "ppv")),
    ]


class IActivateAudioInterfaceAsyncOperation(IUnknown):
    _iid_ = GUID("{72A22D78-CDE4-431D-B8CC-843A71199B6D}")
    _methods_ = [
        COMMETHOD([], HRESULT, "GetActivateResult",
                  (["out"], POINTER(c_int), "activateResult"),
                  (["out"], POINTER(POINTER(IUnknown)), "activatedInterface")),
    ]


class IActivateAudioInterfaceCompletionHandler(IUnknown):
    _iid_ = GUID("{41D949AB-9862-444A-80F6-C261334DA5EB}")
    _methods_ = [
        COMMETHOD([], HRESULT, "ActivateCompleted",
                  (["in"], POINTER(IActivateAudioInterfaceAsyncOperation), "op")),
    ]


class IAgileObject(IUnknown):
    _iid_ = GUID("{94ea2b94-e9cc-49e0-c0ff-ee64ca8f5b90}")
    _methods_ = []


class _CompletionHandler(COMObject):
    _com_interfaces_ = [IActivateAudioInterfaceCompletionHandler, IAgileObject]

    def __init__(self):
        super().__init__()
        self.done = threading.Event()
        self.client = None
        self.error = None

    def ActivateCompleted(self, *args):
        op = args[-1]
        try:
            hr, unk = op.GetActivateResult()
            if hr < 0:
                self.error = OSError(f"Aktivierung fehlgeschlagen (HRESULT 0x{hr & 0xFFFFFFFF:08X})")
            else:
                self.client = unk.QueryInterface(IAudioClient)
        except Exception as e:  # noqa: BLE001
            self.error = e
        finally:
            self.done.set()
        return 0


_mmdev = ctypes.WinDLL("Mmdevapi.dll")
_Activate = _mmdev.ActivateAudioInterfaceAsync
_Activate.argtypes = [c_wchar_p, POINTER(GUID), c_void_p,
                      POINTER(IActivateAudioInterfaceCompletionHandler),
                      POINTER(POINTER(IActivateAudioInterfaceAsyncOperation))]
_Activate.restype = HRESULT

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.CreateEventW.restype = c_void_p
_k32.CreateEventW.argtypes = [c_void_p, c_int, c_int, c_wchar_p]
_k32.WaitForSingleObject.argtypes = [c_void_p, DWORD]
_k32.CloseHandle.argtypes = [c_void_p]


class WindowsCapture:
    """Liefert 44.1 kHz / 16 bit / Stereo PCM an callback(bytes)."""

    def __init__(self, callback, pid=None):
        # pid=None -> ganzer PC-Sound (alles ausser uns selbst)
        self.callback = callback
        self.pid = pid
        self._stop = threading.Event()
        self._thread = None
        self._ready = threading.Event()
        self.error = None
        self.discontinuities = 0

    def start(self):
        self._thread = threading.Thread(target=self._run, name="capture", daemon=True)
        self._thread.start()
        self._ready.wait(10)
        if self.error:
            raise self.error

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(3)

    def _activate(self):
        params = AUDIOCLIENT_ACTIVATION_PARAMS()
        params.ActivationType = AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK
        if self.pid is None:
            params.TargetProcessId = os.getpid()
            params.ProcessLoopbackMode = PROCESS_LOOPBACK_MODE_EXCLUDE
        else:
            params.TargetProcessId = self.pid
            params.ProcessLoopbackMode = PROCESS_LOOPBACK_MODE_INCLUDE
        pv = PROPVARIANT_BLOB()
        pv.vt = VT_BLOB
        pv.cbSize = ctypes.sizeof(params)
        pv.pBlobData = ctypes.cast(ctypes.pointer(params), c_void_p)

        handler = _CompletionHandler()
        hptr = handler.QueryInterface(IActivateAudioInterfaceCompletionHandler)
        op = POINTER(IActivateAudioInterfaceAsyncOperation)()
        _Activate(VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK, ctypes.byref(IAudioClient._iid_),
                  ctypes.byref(pv), hptr, ctypes.byref(op))
        if not handler.done.wait(10):
            raise TimeoutError("Audio-Aktivierung hat nicht geantwortet")
        if handler.error:
            raise handler.error
        return handler.client

    def _run(self):
        comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
        event = None
        try:
            client = self._activate()
            fmt = WAVEFORMATEX(1, CHANNELS, SAMPLE_RATE, SAMPLE_RATE * BYTES_PER_FRAME,
                               BYTES_PER_FRAME, 16, 0)
            flags = (AUDCLNT_STREAMFLAGS_LOOPBACK | AUDCLNT_STREAMFLAGS_EVENTCALLBACK
                     | AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM | AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY)
            client.Initialize(0, flags, 10_000_000, 0, ctypes.byref(fmt), None)  # 1 s Puffer
            event = _k32.CreateEventW(None, 0, 0, None)
            client.SetEventHandle(event)
            cap = client.GetService(ctypes.byref(IAudioCaptureClient._iid_)).QueryInterface(IAudioCaptureClient)
            client.Start()
        except Exception as e:  # noqa: BLE001
            self.error = e
            self._ready.set()
            return
        self._ready.set()
        try:
            while not self._stop.is_set():
                _k32.WaitForSingleObject(event, 100)
                while True:
                    n = cap.GetNextPacketSize()
                    if not n:
                        break
                    data, frames, fl, _, _ = cap.GetBuffer()
                    if frames:
                        size = frames * BYTES_PER_FRAME
                        if fl & 1:  # AUDCLNT_BUFFERFLAGS_DATA_DISCONTINUITY: Daten verpasst
                            self.discontinuities += 1
                        if fl & AUDCLNT_BUFFERFLAGS_SILENT:
                            chunk = bytes(size)
                        else:
                            chunk = ctypes.string_at(data, size)
                        cap.ReleaseBuffer(frames)
                        self.callback(chunk)
                    else:
                        cap.ReleaseBuffer(0)
                        break
        finally:
            try:
                client.Stop()
            except Exception:  # noqa: BLE001
                pass
            if event:
                _k32.CloseHandle(event)


def list_audio_apps():
    """Programme mit aktiver Audio-Session: Liste von (name, pid) – pid ist die Wurzel des Prozessbaums."""
    import psutil
    from pycaw.pycaw import AudioUtilities

    apps = {}
    for s in AudioUtilities.GetAllSessions():
        if not s.Process:
            continue
        try:
            p = psutil.Process(s.Process.pid)
            name = p.name()
            root = p
            # Zur Wurzel gleichen Namens hochlaufen (Browser: Audio-Dienst ist Kindprozess)
            while True:
                par = root.parent()
                if par is None or par.name().lower() != name.lower():
                    break
                root = par
            apps.setdefault(name.lower(), (name, root.pid))
        except psutil.Error:
            continue
    return sorted(apps.values(), key=lambda a: a[0].lower())


def find_app_pid(name):
    """PID fuer einen Programmnamen (z. B. 'chrome' oder 'spotify.exe')."""
    import psutil

    want = name.lower()
    if not want.endswith(".exe"):
        want += ".exe"
    for n, pid in list_audio_apps():
        if n.lower() == want:
            return pid
    # Fallback: Wurzelprozess dieses Namens, auch ohne aktive Session
    for p in psutil.process_iter(["name", "pid"]):
        if (p.info["name"] or "").lower() == want:
            par = p.parent()
            if par is None or (par.name() or "").lower() != want:
                return p.pid
    return None
