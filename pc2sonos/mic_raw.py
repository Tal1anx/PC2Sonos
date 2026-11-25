"""Mikrofon im WASAPI-Raw-Modus (ohne Windows-Effekte wie Voice Clarity /
Rauschunterdrueckung) aufnehmen. Nur fuer PC2Sonos – andere Programme sind
nicht betroffen. Liefert Mono float32 mit 44,1 kHz an callback(array).
"""
import ctypes
import threading
from ctypes import POINTER, Structure, byref, c_int, c_uint32, c_ushort, c_void_p
from ctypes.wintypes import BOOL, DWORD

import comtypes
import numpy as np
from comtypes import COMMETHOD, GUID, HRESULT

from .capture.windows import IAudioCaptureClient, WAVEFORMATEX, _k32

SR = 44100
AUDCLNT_STREAMOPTIONS_RAW = 1
AUDCLNT_STREAMFLAGS_EVENTCALLBACK = 0x00040000
AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM = 0x80000000
AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY = 0x08000000


class AudioClientProperties(Structure):
    _fields_ = [("cbSize", c_uint32), ("bIsOffload", BOOL), ("eCategory", c_int), ("Options", c_int)]


class IAudioClient2(comtypes.IUnknown):
    _iid_ = GUID("{726778CD-F60A-4eda-82DE-E47610CD78AA}")
    _methods_ = [
        COMMETHOD([], HRESULT, "Initialize", (["in"], c_int), (["in"], DWORD), (["in"], ctypes.c_longlong),
                  (["in"], ctypes.c_longlong), (["in"], POINTER(WAVEFORMATEX)), (["in"], POINTER(GUID))),
        COMMETHOD([], HRESULT, "GetBufferSize", (["out"], POINTER(c_uint32))),
        COMMETHOD([], HRESULT, "GetStreamLatency", (["out"], POINTER(ctypes.c_longlong))),
        COMMETHOD([], HRESULT, "GetCurrentPadding", (["out"], POINTER(c_uint32))),
        COMMETHOD([], HRESULT, "IsFormatSupported", (["in"], c_int), (["in"], POINTER(WAVEFORMATEX)),
                  (["out"], POINTER(POINTER(WAVEFORMATEX)))),
        COMMETHOD([], HRESULT, "GetMixFormat", (["out"], POINTER(POINTER(WAVEFORMATEX)))),
        COMMETHOD([], HRESULT, "GetDevicePeriod", (["out"], POINTER(ctypes.c_longlong)),
                  (["out"], POINTER(ctypes.c_longlong))),
        COMMETHOD([], HRESULT, "Start"),
        COMMETHOD([], HRESULT, "Stop"),
        COMMETHOD([], HRESULT, "Reset"),
        COMMETHOD([], HRESULT, "SetEventHandle", (["in"], c_void_p)),
        COMMETHOD([], HRESULT, "GetService", (["in"], POINTER(GUID)), (["out"], POINTER(POINTER(comtypes.IUnknown)))),
        # IAudioClient2
        COMMETHOD([], HRESULT, "IsOffloadCapable", (["in"], c_int), (["out"], POINTER(BOOL))),
        COMMETHOD([], HRESULT, "SetClientProperties", (["in"], POINTER(AudioClientProperties))),
        COMMETHOD([], HRESULT, "GetBufferSizeLimits", (["in"], POINTER(WAVEFORMATEX)), (["in"], BOOL),
                  (["out"], POINTER(ctypes.c_longlong)), (["out"], POINTER(ctypes.c_longlong))),
    ]


def _default_mic():
    from pycaw.api.mmdeviceapi import IMMDeviceEnumerator
    from pycaw.constants import EDataFlow, ERole
    en = comtypes.CoCreateInstance(GUID("{BCDE0395-E52F-467C-8E3D-C4579291692E}"), IMMDeviceEnumerator,
                                   comtypes.CLSCTX_INPROC_SERVER)
    return en.GetDefaultAudioEndpoint(EDataFlow.eCapture.value, ERole.eConsole.value)


class RawMic:
    def __init__(self, callback):
        self.callback = callback
        self._stop = threading.Event()
        self._ready = threading.Event()
        self.error = None
        self.raw = False
        self.discontinuities = 0

    def start(self):
        self._t = threading.Thread(target=self._run, name="rawmic", daemon=True)
        self._t.start()
        self._ready.wait(5)
        if self.error:
            raise self.error

    def stop(self):
        self._stop.set()
        self._t.join(2)

    def _run(self):
        comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
        try:
            dev = _default_mic()
            client = dev.Activate(IAudioClient2._iid_, comtypes.CLSCTX_ALL, None).QueryInterface(IAudioClient2)
            props = AudioClientProperties(ctypes.sizeof(AudioClientProperties), 0, 0, AUDCLNT_STREAMOPTIONS_RAW)
            try:
                client.SetClientProperties(byref(props))
                self.raw = True
            except Exception:  # noqa: BLE001  Raw nicht unterstuetzt -> normal weiter
                self.raw = False
            fmt = WAVEFORMATEX(1, 1, SR, SR * 2, 2, 16, 0)  # mono 16 bit, Windows rechnet um
            client.Initialize(0, AUDCLNT_STREAMFLAGS_EVENTCALLBACK | AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM
                              | AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY, 10_000_000, 0, byref(fmt), None)  # 1 s
            ev = _k32.CreateEventW(None, 0, 0, None)
            client.SetEventHandle(ev)
            cap = client.GetService(byref(IAudioCaptureClient._iid_)).QueryInterface(IAudioCaptureClient)
            client.Start()
        except Exception as e:  # noqa: BLE001
            self.error = e
            self._ready.set()
            return
        self._ready.set()
        try:
            while not self._stop.is_set():
                _k32.WaitForSingleObject(ev, 100)
                while cap.GetNextPacketSize():
                    data, frames, flags, _, _ = cap.GetBuffer()
                    if frames:
                        a = np.frombuffer(ctypes.string_at(data, frames * 2), dtype="<i2").astype(np.float32) / 32768
                        if flags & 1:  # Daten verpasst
                            self.discontinuities += 1
                        if flags & 2:
                            a[:] = 0
                        cap.ReleaseBuffer(frames)
                        self.callback(a)
                    else:
                        cap.ReleaseBuffer(0)
                        break
        finally:
            client.Stop()
            _k32.CloseHandle(ev)
