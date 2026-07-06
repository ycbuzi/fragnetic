"""Windows Graphics Capture (WGC) -- record ONLY the FragPunk WINDOW, so overlays
(browser tabs, Discord, the app itself) that sit on top of the game never land in the
clip. The ddagrab recorder captures the whole monitor's composited image, so anything
over the game is baked in; WGC captures the target window's own surface instead.

Pure ctypes (no winsdk/opencv dependency -- the shipped exe stays lean). WinRT
activation + IGraphicsCaptureItemInterop::CreateForWindow are proven working; this adds
the D3D11 device, a free-threaded frame pool, a capture session, and a single-frame
grab that copies the captured texture to a CPU-readable staging texture and returns raw
BGRA bytes. That single-frame path is the foundation the continuous recorder builds on.

Windows 10 1903+ (WGC). Everything is wrapped so any failure returns None and the caller
falls back to the existing ddagrab/desktop path -- the recorder can only be upgraded.
"""
import os
import time

_IS_WIN = os.name == "nt"

# DirectXPixelFormat.B8G8R8A8UIntNormalized
_PIXFMT_BGRA8 = 87
# D3D
_D3D_DRIVER_HARDWARE = 1
_D3D11_CREATE_BGRA_SUPPORT = 0x20
_D3D11_SDK_VERSION = 7
_D3D11_USAGE_STAGING = 3
_D3D11_CPU_ACCESS_READ = 0x20000
_D3D11_MAP_READ = 1
_RO_INIT_MULTITHREADED = 1


def available():
    """True if this OS can do WGC (Win10 1903+) and the DLLs/symbols load."""
    if not _IS_WIN:
        return False
    try:
        import ctypes
        ctypes.WinDLL("combase.dll")
        ctypes.WinDLL("d3d11.dll")
        return True
    except Exception:
        return False


def find_fragpunk_hwnd(pids=None):
    """The FragPunk game window handle (HWND as int), or None. Matches the visible
    top-level window owned by a game PID, preferring the one titled 'FragPunk'."""
    if not _IS_WIN:
        return None
    try:
        import ctypes
        from ctypes import wintypes
        u32 = ctypes.WinDLL("user32", use_last_error=True)
        want = set(int(p) for p in (pids or []))
        best = [None, None]  # [titled 'FragPunk', any game-pid window]

        @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
        def _cb(hwnd, _lp):
            if not u32.IsWindowVisible(hwnd):
                return True
            pid = wintypes.DWORD()
            u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if want and pid.value not in want:
                return True
            n = u32.GetWindowTextLengthW(hwnd)
            buf = ctypes.create_unicode_buffer(n + 1)
            u32.GetWindowTextW(hwnd, buf, n + 1)
            title = buf.value or ""
            # skip zero-area windows (message-only / hidden helpers)
            r = wintypes.RECT()
            u32.GetWindowRect(hwnd, ctypes.byref(r))
            if (r.right - r.left) < 100 or (r.bottom - r.top) < 100:
                return True
            if title.strip().lower() == "fragpunk":
                best[0] = int(hwnd)
            elif best[1] is None:
                best[1] = int(hwnd)
            return True

        u32.EnumWindows(_cb, 0)
        return best[0] or best[1]
    except Exception:
        return None


# --------------------------------------------------------------------------- #
#  ctypes COM/WinRT plumbing
# --------------------------------------------------------------------------- #
def _guid(ctypes, d1, d2, d3, tail):
    class GUID(ctypes.Structure):
        _fields_ = [("Data1", ctypes.c_uint32), ("Data2", ctypes.c_uint16),
                    ("Data3", ctypes.c_uint16), ("Data4", ctypes.c_ubyte * 8)]
    g = GUID()
    g.Data1, g.Data2, g.Data3 = d1, d2, d3
    for i, b in enumerate(tail):
        g.Data4[i] = b
    return g


def _method(ctypes, ptr, index, restype, *argtypes):
    """Bind vtable slot `index` of the COM/WinRT interface at `ptr`."""
    vt = ctypes.cast(ptr, ctypes.POINTER(ctypes.c_void_p)).contents.value
    fn = ctypes.cast(vt, ctypes.POINTER(ctypes.c_void_p))[index]
    proto = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)
    f = proto(fn)
    return lambda *a: f(ptr, *a)


def grab_frame(hwnd, timeout_s=2.0):
    """Capture ONE frame of the given window via WGC and return (width, height,
    bgra_bytes) or None on any failure. bgra_bytes is tightly packed width*height*4
    (row padding removed). This validates the full pipeline and is also the per-frame
    primitive the continuous recorder will reuse."""
    if not _IS_WIN or not hwnd:
        return None
    try:
        import ctypes
        from ctypes import wintypes, byref, c_void_p, POINTER
    except Exception:
        return None

    combase = ctypes.WinDLL("combase.dll")
    d3d11 = ctypes.WinDLL("d3d11.dll")
    c_long = ctypes.c_long

    # ---- IIDs ----
    IID_ItemInterop = _guid(ctypes, 0x3628E81B, 0x3CAC, 0x4C60,
                            (0xB7, 0xF4, 0x23, 0xCE, 0x0E, 0x0C, 0x33, 0x56))
    IID_Item = _guid(ctypes, 0x79C3F95B, 0x31F7, 0x4EC2,
                     (0xA4, 0x64, 0x63, 0x2E, 0xF5, 0xD3, 0x07, 0x60))
    IID_PoolStatics2 = _guid(ctypes, 0x589B103F, 0x6BBC, 0x5DF5,
                             (0xA9, 0x91, 0x02, 0xE2, 0x8B, 0x3B, 0x66, 0xD5))
    IID_DXGIDevice = _guid(ctypes, 0x54EC77FA, 0x1377, 0x44E6,
                           (0x8C, 0x32, 0x88, 0xFD, 0x5F, 0x44, 0xC8, 0x4C))
    IID_Direct3DDevice = _guid(ctypes, 0xA37624AB, 0x8D5F, 0x4650,
                               (0x9D, 0x3E, 0x9E, 0xAE, 0x3D, 0x9B, 0xC6, 0x70))
    IID_DxgiIfaceAccess = _guid(ctypes, 0xA9B3D012, 0x3DF2, 0x4EE3,
                                (0xB8, 0xD1, 0x86, 0x95, 0xF4, 0x57, 0xD3, 0xC1))
    IID_Texture2D = _guid(ctypes, 0x6F15AAF2, 0xD208, 0x4E89,
                          (0x9A, 0xB4, 0x48, 0x95, 0x35, 0xD3, 0x4F, 0x9C))

    def _hstring(s):
        h = c_void_p()
        combase.WindowsCreateString.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32,
                                                POINTER(c_void_p)]
        combase.WindowsCreateString(s, len(s), byref(h))
        return h

    class SizeInt32(ctypes.Structure):
        _fields_ = [("W", ctypes.c_int32), ("H", ctypes.c_int32)]

    class TEX2D_DESC(ctypes.Structure):
        _fields_ = [("Width", ctypes.c_uint32), ("Height", ctypes.c_uint32),
                    ("MipLevels", ctypes.c_uint32), ("ArraySize", ctypes.c_uint32),
                    ("Format", ctypes.c_uint32),
                    ("SampleCount", ctypes.c_uint32), ("SampleQuality", ctypes.c_uint32),
                    ("Usage", ctypes.c_uint32), ("BindFlags", ctypes.c_uint32),
                    ("CPUAccessFlags", ctypes.c_uint32), ("MiscFlags", ctypes.c_uint32)]

    class MAPPED(ctypes.Structure):
        _fields_ = [("pData", c_void_p), ("RowPitch", ctypes.c_uint32),
                    ("DepthPitch", ctypes.c_uint32)]

    device = c_void_p()
    context = c_void_p()
    d3ddev = c_void_p()
    item = c_void_p()
    pool = c_void_p()
    session = c_void_p()
    frame = c_void_p()
    tex = c_void_p()
    staging = c_void_p()
    ro_inited = False
    mapped_res = None
    result = None
    try:
        combase.RoInitialize.restype = c_long
        hr = combase.RoInitialize(_RO_INIT_MULTITHREADED)
        ro_inited = (hr >= 0)

        # ---- D3D11 device + context ----
        d3d11.D3D11CreateDevice.restype = c_long
        d3d11.D3D11CreateDevice.argtypes = [c_void_p, ctypes.c_int, c_void_p,
            ctypes.c_uint, c_void_p, ctypes.c_uint, ctypes.c_uint,
            POINTER(c_void_p), c_void_p, POINTER(c_void_p)]
        if d3d11.D3D11CreateDevice(None, _D3D_DRIVER_HARDWARE, None,
                                   _D3D11_CREATE_BGRA_SUPPORT, None, 0, _D3D11_SDK_VERSION,
                                   byref(device), None, byref(context)) < 0:
            return None

        # ID3D11Device -> IDXGIDevice (QueryInterface, slot 0)
        dxgi = c_void_p()
        if _method(ctypes, device, 0, c_long, POINTER(type(IID_DXGIDevice)),
                   POINTER(c_void_p))(byref(IID_DXGIDevice), byref(dxgi)) < 0:
            return None
        # wrap as WinRT IDirect3DDevice
        d3d11.CreateDirect3D11DeviceFromDXGIDevice.restype = c_long
        d3d11.CreateDirect3D11DeviceFromDXGIDevice.argtypes = [c_void_p, POINTER(c_void_p)]
        inspectable = c_void_p()
        if d3d11.CreateDirect3D11DeviceFromDXGIDevice(dxgi, byref(inspectable)) < 0:
            return None
        _method(ctypes, dxgi, 2, ctypes.c_ulong)()   # release IDXGIDevice
        if _method(ctypes, inspectable, 0, c_long, POINTER(type(IID_Direct3DDevice)),
                   POINTER(c_void_p))(byref(IID_Direct3DDevice), byref(d3ddev)) < 0:
            return None
        _method(ctypes, inspectable, 2, ctypes.c_ulong)()

        # ---- capture item for the window ----
        interop = c_void_p()
        combase.RoGetActivationFactory.restype = c_long
        combase.RoGetActivationFactory.argtypes = [c_void_p, c_void_p, POINTER(c_void_p)]
        hs_item = _hstring("Windows.Graphics.Capture.GraphicsCaptureItem")
        if combase.RoGetActivationFactory(hs_item, byref(IID_ItemInterop),
                                          byref(interop)) < 0:
            return None
        # IGraphicsCaptureItemInterop::CreateForWindow (slot 3)
        if _method(ctypes, interop, 3, c_long, wintypes.HWND,
                   POINTER(type(IID_Item)), POINTER(c_void_p))(
                   hwnd, byref(IID_Item), byref(item)) < 0 or not item:
            return None
        # item size (slot 7)
        size = SizeInt32()
        if _method(ctypes, item, 7, c_long, POINTER(SizeInt32))(byref(size)) < 0:
            return None
        w, h = size.W, size.H
        if w <= 0 or h <= 0:
            return None

        # ---- free-threaded frame pool ----
        pstat = c_void_p()
        hs_pool = _hstring("Windows.Graphics.Capture.Direct3D11CaptureFramePool")
        if combase.RoGetActivationFactory(hs_pool, byref(IID_PoolStatics2),
                                          byref(pstat)) < 0 or not pstat:
            return None
        # IDirect3D11CaptureFramePoolStatics2::CreateFreeThreaded (slot 6):
        #   (IDirect3DDevice*, DirectXPixelFormat, INT32 numberOfBuffers, SizeInt32)
        if _method(ctypes, pstat, 6, c_long, c_void_p, ctypes.c_int, ctypes.c_int32,
                   SizeInt32, POINTER(c_void_p))(
                   d3ddev, _PIXFMT_BGRA8, 2, size, byref(pool)) < 0 or not pool:
            return None
        _method(ctypes, pstat, 2, ctypes.c_ulong)()

        # session (slot 10 CreateCaptureSession)
        if _method(ctypes, pool, 10, c_long, c_void_p, POINTER(c_void_p))(
                item, byref(session)) < 0 or not session:
            return None
        # Best-effort: kill WGC's default YELLOW capture border and cursor before we
        # start -- a border drawn around the game during a match would be maddening,
        # and we don't want the mouse baked into gameplay clips. Both are on newer
        # session interfaces (Win10 2004+/Win11); ignore if unsupported.
        _s2 = _guid(ctypes, 0x2C39AE40, 0x7D2E, 0x5044,
                    (0x80, 0x4E, 0x8B, 0x67, 0x99, 0xD4, 0xCF, 0x9E))  # IGraphicsCaptureSession2
        _s3 = _guid(ctypes, 0xF2CDD966, 0x22AE, 0x5EA1,
                    (0x95, 0x96, 0x3A, 0x28, 0x93, 0x44, 0xC3, 0xBE))  # IGraphicsCaptureSession3
        for _iid, _slot in ((_s2, 7), (_s3, 7)):   # put_IsCursorCaptureEnabled / put_IsBorderRequired
            try:
                _ss = c_void_p()
                if _method(ctypes, session, 0, c_long, POINTER(type(_iid)),
                           POINTER(c_void_p))(byref(_iid), byref(_ss)) >= 0 and _ss.value:
                    _method(ctypes, _ss, _slot, c_long, ctypes.c_byte)(0)   # set False
                    _method(ctypes, _ss, 2, ctypes.c_ulong)()              # release
            except Exception:
                pass
        _method(ctypes, session, 6, c_long)()   # StartCapture

        # ---- poll for a frame (slot 7 TryGetNextFrame) ----
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            frame = c_void_p()
            _method(ctypes, pool, 7, c_long, POINTER(c_void_p))(byref(frame))
            if frame.value:
                break
            time.sleep(0.01)
        if not frame or not frame.value:
            return None

        # frame->get_Surface (slot 6) -> QI IDirect3DDxgiInterfaceAccess ->
        # GetInterface(ID3D11Texture2D) (slot 3)
        surface = c_void_p()
        if _method(ctypes, frame, 6, c_long, POINTER(c_void_p))(byref(surface)) < 0:
            return None
        access = c_void_p()
        if _method(ctypes, surface, 0, c_long, POINTER(type(IID_DxgiIfaceAccess)),
                   POINTER(c_void_p))(byref(IID_DxgiIfaceAccess), byref(access)) < 0:
            return None
        if _method(ctypes, access, 3, c_long, POINTER(type(IID_Texture2D)),
                   POINTER(c_void_p))(byref(IID_Texture2D), byref(tex)) < 0 or not tex:
            return None

        # describe the captured texture (ID3D11Texture2D::GetDesc, slot 10), then make
        # a CPU-readable STAGING copy and map it.
        desc = TEX2D_DESC()
        _method(ctypes, tex, 10, None, POINTER(TEX2D_DESC))(byref(desc))
        desc.Usage = _D3D11_USAGE_STAGING
        desc.CPUAccessFlags = _D3D11_CPU_ACCESS_READ
        desc.BindFlags = 0
        desc.MiscFlags = 0
        # ID3D11Device::CreateTexture2D (slot 5)
        if _method(ctypes, device, 5, c_long, POINTER(TEX2D_DESC), c_void_p,
                   POINTER(c_void_p))(byref(desc), None, byref(staging)) < 0 or not staging:
            return None
        # ID3D11DeviceContext::CopyResource (slot 47), Map (14), Unmap (15)
        _method(ctypes, context, 47, None, c_void_p, c_void_p)(staging, tex)
        m = MAPPED()
        if _method(ctypes, context, 14, c_long, c_void_p, ctypes.c_uint, ctypes.c_int,
                   ctypes.c_uint, POINTER(MAPPED))(staging, 0, _D3D11_MAP_READ, 0,
                                                   byref(m)) < 0:
            return None
        mapped_res = staging
        # copy row-by-row, stripping the RowPitch padding -> tight width*4 BGRA rows
        rowbytes = w * 4
        base = m.pData
        pitch = m.RowPitch
        rows = [ctypes.string_at(base + y * pitch, rowbytes) for y in range(h)]
        result = (w, h, b"".join(rows))
    except Exception:
        result = None
    finally:
        try:
            if mapped_res is not None:
                _method(ctypes, context, 15, None, c_void_p, ctypes.c_uint)(mapped_res, 0)
        except Exception:
            pass
        for p in (staging, tex, frame, session, pool, item, d3ddev, context, device):
            try:
                if p and p.value:
                    _method(ctypes, p, 2, ctypes.c_ulong)()   # Release
            except Exception:
                pass
        if ro_inited:
            try:
                combase.RoUninitialize()
            except Exception:
                pass
    return result
