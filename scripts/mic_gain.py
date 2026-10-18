"""Read or set a capture device's master level through Core Audio.

The speakerphone is being driven into full-scale clipping (peak 1.0000), which its echo
canceller then mangles. Lowering the Windows capture level fixes it at source.

    python mic_gain.py                 show every capture device's level
    python mic_gain.py webcam 0.55     set the one matching "webcam" to 55%
"""
import ctypes
import sys
from ctypes import POINTER, byref, c_float, c_uint, c_void_p
from ctypes.wintypes import DWORD, LPCWSTR

ole32 = ctypes.windll.ole32


class GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort),
                ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8)]

    def __init__(self, s):
        super().__init__()
        ole32.CLSIDFromString(LPCWSTR(s), byref(self))


class PROPERTYKEY(ctypes.Structure):
    _fields_ = [("fmtid", GUID), ("pid", DWORD)]


CLSID_MMDeviceEnumerator = GUID("{BCDE0395-E52F-467C-8E3D-C4579291692E}")
IID_IMMDeviceEnumerator = GUID("{A95664D2-9614-4F35-A746-DE8DB63617E6}")
IID_IAudioEndpointVolume = GUID("{5CDF2C82-841E-4546-9722-0CF74078229A}")
IID_IPropertyStore = GUID("{886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99}")
PKEY_FriendlyName = PROPERTYKEY()
PKEY_FriendlyName.fmtid = GUID("{A45C254E-DF1C-4EFD-8020-67D146A850E0}")
PKEY_FriendlyName.pid = 14

eCapture = 1
DEVICE_STATE_ACTIVE = 1
CLSCTX_ALL = 23
STGM_READ = 0


def vt(ptr, index, restype, *argtypes):
    """Call slot `index` of a COM object's vtable."""
    vtable = ctypes.cast(ptr, POINTER(POINTER(c_void_p))).contents
    proto = ctypes.WINFUNCTYPE(restype, c_void_p, *argtypes)
    return proto(vtable[index])


class PROPVARIANT(ctypes.Structure):
    _fields_ = [("vt", ctypes.c_ushort), ("r1", ctypes.c_ushort),
                ("r2", ctypes.c_ushort), ("r3", ctypes.c_ushort),
                ("p", c_void_p), ("pad", c_void_p)]


def devices():
    ole32.CoInitialize(None)
    enum = c_void_p()
    hr = ole32.CoCreateInstance(byref(CLSID_MMDeviceEnumerator), None, CLSCTX_ALL,
                                byref(IID_IMMDeviceEnumerator), byref(enum))
    if hr:
        raise OSError("CoCreateInstance 0x%08X" % (hr & 0xFFFFFFFF))
    coll = c_void_p()
    # IMMDeviceEnumerator::EnumAudioEndpoints is vtable slot 3
    vt(enum, 3, ctypes.HRESULT, DWORD, DWORD, POINTER(c_void_p))(
        enum, eCapture, DEVICE_STATE_ACTIVE, byref(coll))
    n = c_uint()
    vt(coll, 3, ctypes.HRESULT, POINTER(c_uint))(coll, byref(n))   # GetCount
    out = []
    for i in range(n.value):
        dev = c_void_p()
        vt(coll, 4, ctypes.HRESULT, c_uint, POINTER(c_void_p))(coll, i, byref(dev))
        store = c_void_p()
        vt(dev, 4, ctypes.HRESULT, DWORD, POINTER(c_void_p))(dev, STGM_READ, byref(store))
        pv = PROPVARIANT()
        vt(store, 5, ctypes.HRESULT, POINTER(PROPERTYKEY), POINTER(PROPVARIANT))(
            store, byref(PKEY_FriendlyName), byref(pv))
        name = ctypes.wstring_at(pv.p) if pv.p else "(unnamed)"
        vol = c_void_p()
        # IMMDevice::Activate is slot 3
        vt(dev, 3, ctypes.HRESULT, POINTER(GUID), DWORD, c_void_p, POINTER(c_void_p))(
            dev, byref(IID_IAudioEndpointVolume), CLSCTX_ALL, None, byref(vol))
        out.append((name, vol))
    return out


def get_level(vol):
    v = c_float()
    # IAudioEndpointVolume::GetMasterVolumeLevelScalar is slot 9
    vt(vol, 9, ctypes.HRESULT, POINTER(c_float))(vol, byref(v))
    return v.value


def set_level(vol, value):
    # SetMasterVolumeLevelScalar is slot 7
    hr = vt(vol, 7, ctypes.HRESULT, c_float, POINTER(GUID))(vol, c_float(value), None)
    return hr == 0


if __name__ == "__main__":
    devs = devices()
    if len(sys.argv) < 3:
        print("  capture devices and their levels:")
        for name, vol in devs:
            try:
                print("    %-48s %5.1f%%" % (name[:48], get_level(vol) * 100))
            except Exception as e:
                print("    %-48s  (level unavailable: %s)" % (name[:48], e))
        sys.exit(0)

    want, target = sys.argv[1].lower(), float(sys.argv[2])
    for name, vol in devs:
        if want in name.lower():
            before = get_level(vol)
            ok = set_level(vol, target)
            after = get_level(vol)
            print("  %s" % name)
            print("    was  %5.1f%%" % (before * 100))
            print("    now  %5.1f%%   (set %s)" % (after * 100, "ok" if ok else "FAILED"))
            sys.exit(0 if ok else 1)
    print("  no capture device matching %r" % want)
    sys.exit(1)
