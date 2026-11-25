"""Windows: Ausgabegeraet pro Programm setzen (wie im Lautstaerkemixer).

Nutzt die interne WinRT-Klasse Windows.Media.Internal.AudioPolicyConfig
(IAudioPolicyConfigFactory, Windows 10 21H2+ / 11) – dieselbe Schnittstelle,
die der Windows-Lautstaerkemixer und EarTrumpet verwenden. Betrifft nur das
gewaehlte Programm, nicht das Standardgeraet.
"""
import ctypes
from ctypes import HRESULT, POINTER, WINFUNCTYPE, byref, c_uint, c_void_p, c_wchar_p

from comtypes import GUID

_combase = ctypes.WinDLL("combase.dll")
_combase.WindowsCreateString.argtypes = [c_wchar_p, c_uint, POINTER(c_void_p)]
_combase.WindowsCreateString.restype = HRESULT
_combase.WindowsDeleteString.argtypes = [c_void_p]
_combase.WindowsGetStringRawBuffer.argtypes = [c_void_p, POINTER(c_uint)]
_combase.WindowsGetStringRawBuffer.restype = c_wchar_p
_combase.RoGetActivationFactory.argtypes = [c_void_p, POINTER(GUID), POINTER(c_void_p)]
_combase.RoGetActivationFactory.restype = HRESULT
_combase.RoInitialize.argtypes = [c_uint]

IID_FACTORY = GUID("{ab3d4648-e242-459f-b02f-541c70306324}")  # 21H2+
SLOT_SET = 25  # IUnknown(3) + IInspectable(3) + 19 Platzhalter
SLOT_GET = 26
E_RENDER = 0
ROLES = (0, 1)  # eConsole, eMultimedia
RENDER_PREFIX = "\\\\?\\SWD#MMDEVAPI#"
RENDER_SUFFIX = "#{e6327cad-dcec-4949-ae8a-991e976a79d2}"

_SetProto = WINFUNCTYPE(HRESULT, c_void_p, c_uint, c_uint, c_uint, c_void_p)
_GetProto = WINFUNCTYPE(HRESULT, c_void_p, c_uint, c_uint, c_uint, POINTER(c_void_p))


def _hstring(s):
    h = c_void_p()
    if s:
        _combase.WindowsCreateString(s, len(s), byref(h))
    return h


def _factory():
    try:
        _combase.RoInitialize(1)  # MTA; schon initialisiert ist ok
    except OSError:
        pass
    name = _hstring("Windows.Media.Internal.AudioPolicyConfig")
    f = c_void_p()
    try:
        _combase.RoGetActivationFactory(name, byref(IID_FACTORY), byref(f))
    finally:
        _combase.WindowsDeleteString(name)
    return f


def _vfunc(obj, slot, proto):
    vtbl = ctypes.cast(obj, POINTER(POINTER(c_void_p))).contents
    return proto(vtbl[slot])


def _release(obj):
    _vfunc(obj, 2, WINFUNCTYPE(c_uint, c_void_p))(obj)


def list_output_devices():
    """[(endpoint_id, name)] aller aktiven Ausgabegeraete."""
    from pycaw.pycaw import AudioUtilities
    from pycaw.constants import EDataFlow, DEVICE_STATE

    out = []
    for d in AudioUtilities.GetAllDevices(data_flow=EDataFlow.eRender.value,
                                         device_state=DEVICE_STATE.ACTIVE.value):
        out.append((d.id, d.FriendlyName))
    return out


def get_app_output(pid):
    """Endpoint-ID, auf die das Programm gezielt umgeleitet ist (None = Standard).
    Wirft OSError fuer Prozesse ohne Audio (z. B. Chrome-Sandbox-Prozesse)."""
    f = _factory()
    try:
        h = c_void_p()
        _vfunc(f, SLOT_GET, _GetProto)(f, pid, E_RENDER, 1, byref(h))
        if not h.value:
            return None
        s = _combase.WindowsGetStringRawBuffer(h, None)
        _combase.WindowsDeleteString(h)
        if s.startswith(RENDER_PREFIX):
            s = s[len(RENDER_PREFIX):]
        if s.endswith(RENDER_SUFFIX):
            s = s[: -len(RENDER_SUFFIX)]
        return s or None
    finally:
        _release(f)


def set_app_output(pids, endpoint_id):
    """Programm(e) auf ein Ausgabegeraet legen; endpoint_id=None -> zurueck auf Standard."""
    f = _factory()
    try:
        setter = _vfunc(f, SLOT_SET, _SetProto)
        h = _hstring(f"{RENDER_PREFIX}{endpoint_id}{RENDER_SUFFIX}" if endpoint_id else None)
        try:
            for pid in pids:
                for role in ROLES:
                    try:
                        setter(f, pid, E_RENDER, role, h)
                    except OSError:
                        pass  # Prozess ohne Audio
        finally:
            if h.value:
                _combase.WindowsDeleteString(h)
    finally:
        _release(f)


def get_app_output_any(pids):
    """Erste gesetzte Umleitung unter den PIDs (None = Standard)."""
    for pid in pids:
        try:
            dev = get_app_output(pid)
        except OSError:
            continue
        if dev:
            return dev
    return None


def pick_dummy_output(exclude_name=None):
    """Ungenutztes Ausgabegeraet fuer die Umleitung: bevorzugt digitale/SPDIF-Ausgaenge."""
    devs = [(i, n) for i, n in list_output_devices() if n != exclude_name]
    for i, n in devs:
        if any(k in n.lower() for k in ("digital", "spdif", "s/pdif")):
            return i, n
    return devs[0] if devs else (None, None)


def app_pids(exe_name):
    import psutil

    want = exe_name.lower()
    return [p.pid for p in psutil.process_iter(["name"]) if (p.info["name"] or "").lower() == want]
