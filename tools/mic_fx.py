"""Audioverbesserungen (System-Effekte, z. B. KI-Rauschunterdrueckung) eines
Aufnahmegeraets ein-/ausschalten – wie der Schalter in den Windows-Einstellungen.

    python tools\\mic_fx.py status
    python tools\\mic_fx.py off      # Effekte aus
    python tools\\mic_fx.py on       # Effekte wieder an (Ursprungszustand)

Nutzt die interne Schnittstelle IPolicyConfig (wie Systemsteuerung/Einstellungen).
"""
import ctypes
import sys
from ctypes import HRESULT, POINTER, WINFUNCTYPE, Structure, byref, c_int, c_ubyte, c_uint, c_ushort, c_void_p, c_wchar_p

import comtypes
from comtypes import GUID

CLSID_PolicyConfig = GUID("{870af99c-171d-4f9e-af0d-e63df40c2bc9}")
IID_IPolicyConfig = GUID("{f8679f50-850a-41cf-9c72-430f290290c8}")
SLOT_GET, SLOT_SET = 11, 12
VT_UI4 = 19


class PROPERTYKEY(Structure):
    _fields_ = [("fmtid", GUID), ("pid", c_uint)]


class PROPVARIANT(Structure):
    _fields_ = [("vt", c_ushort), ("r1", c_ushort), ("r2", c_ushort), ("r3", c_ushort),
                ("ulVal", c_uint), ("pad", c_uint), ("pad2", c_void_p)]


PKEY_Disable_SysFx = PROPERTYKEY(GUID("{1da5d803-d492-4edd-8c23-e0c0ffee7f0e}"), 5)


def _policy():
    comtypes.CoInitialize()
    p = c_void_p()
    ole32 = ctypes.OleDLL("ole32")
    ole32.CoCreateInstance(byref(CLSID_PolicyConfig), None, 1 | 4 | 16, byref(IID_IPolicyConfig), byref(p))
    return p


def _call(p, slot, *args):
    vtbl = ctypes.cast(p, POINTER(POINTER(c_void_p))).contents
    proto = WINFUNCTYPE(HRESULT, c_void_p, c_wchar_p, c_int, POINTER(PROPERTYKEY), POINTER(PROPVARIANT))
    return proto(vtbl[slot])(p, *args)


def default_mic_id():
    from pycaw.pycaw import AudioUtilities
    from pycaw.constants import EDataFlow, ERole
    import comtypes as ct
    from pycaw.api.mmdeviceapi import IMMDeviceEnumerator
    CLSID_MMDeviceEnumerator = GUID("{BCDE0395-E52F-467C-8E3D-C4579291692E}")
    en = ct.CoCreateInstance(CLSID_MMDeviceEnumerator, IMMDeviceEnumerator, ct.CLSCTX_INPROC_SERVER)
    dev = en.GetDefaultAudioEndpoint(EDataFlow.eCapture.value, ERole.eCommunications.value)
    return dev.GetId()


def get(dev_id):
    p = _policy()
    v = PROPVARIANT()
    _call(p, SLOT_GET, dev_id, 1, byref(PKEY_Disable_SysFx), byref(v))
    return v.ulVal if v.vt == VT_UI4 else None


def set_disabled(dev_id, disabled: bool):
    p = _policy()
    v = PROPVARIANT()
    v.vt = VT_UI4
    v.ulVal = 1 if disabled else 0
    _call(p, SLOT_SET, dev_id, 1, byref(PKEY_Disable_SysFx), byref(v))


if __name__ == "__main__":
    dev = sys.argv[2] if len(sys.argv) > 2 else default_mic_id()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "off":
        set_disabled(dev, True)
    elif cmd == "on":
        set_disabled(dev, False)
    val = get(dev)
    print(f"{dev}: Effekte {'AUS' if val == 1 else 'AN'} (Disable_SysFx={val})")
