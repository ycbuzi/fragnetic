"""Windows Graphics Capture (WGC) -- record ONLY the FragPunk WINDOW, so overlays
(browser tabs, Discord, the app itself) that sit on top of the game never land in the
clip. The ddagrab recorder captures the whole monitor's composited image, so anything
over the game is baked in; WGC captures the target window's own surface instead.

Pure ctypes (no winsdk/opencv dependency -- the shipped exe stays lean). WinRT
activation + IGraphicsCaptureItemInterop::CreateForWindow open a capture item for the
window; a D3D11 device + free-threaded frame pool + capture session deliver frames as
D3D11 textures, which we copy to a CPU-readable staging texture and read as BGRA.

  find_fragpunk_hwnd(pids)     -> the game window handle (int) or None
  grab_frame(hwnd)             -> (w, h, bgra_bytes) single frame (validation/probe)
  capture_clip(hwnd, out, ...) -> continuous capture piped to ffmpeg/NVENC (the recorder)

Session setup is separated from per-frame grab so the recorder pulls frames in a tight
loop without re-initializing. Any failure returns None/False so the caller falls back to
the existing ddagrab/desktop path -- the recorder can only be upgraded. Win10 1903+.
"""
import os
import subprocess
import time

_IS_WIN = os.name == "nt"

_PIXFMT_BGRA8 = 87                 # DirectXPixelFormat.B8G8R8A8UIntNormalized
_DXGI_B8G8R8A8_UNORM = 87         # DXGI_FORMAT_B8G8R8A8_UNORM (same value)
_D3D_DRIVER_HARDWARE = 1
_D3D11_CREATE_BGRA_SUPPORT = 0x20
_D3D11_SDK_VERSION = 7
_D3D11_USAGE_STAGING = 3
_D3D11_CPU_ACCESS_READ = 0x20000
_D3D11_MAP_READ = 1
_RO_INIT_MULTITHREADED = 1
_NO_WINDOW = 0x08000000


def available():
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
    """The FragPunk game window handle (HWND as int), or None. Prefers the visible
    top-level window titled 'FragPunk'; falls back to any sizable game-PID window."""
    if not _IS_WIN:
        return None
    try:
        import ctypes
        from ctypes import wintypes
        u32 = ctypes.WinDLL("user32", use_last_error=True)
        want = set(int(p) for p in (pids or []))
        best = [None, None]

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
            r = wintypes.RECT()
            u32.GetWindowRect(hwnd, ctypes.byref(r))
            if (r.right - r.left) < 100 or (r.bottom - r.top) < 100:
                return True
            if title.strip().lower() == "fragpunk":
                best[0] = int(hwnd)
            elif best[1] is None and want:
                # only accept a non-'FragPunk'-titled window as a fallback when we're
                # filtering by the game's PIDs -- never grab an arbitrary window by size.
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
    vt = ctypes.cast(ptr, ctypes.POINTER(ctypes.c_void_p)).contents.value
    fn = ctypes.cast(vt, ctypes.POINTER(ctypes.c_void_p))[index]
    proto = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)
    f = proto(fn)
    return lambda *a: f(ptr, *a)


def _iids(ctypes):
    return {
        "interop": _guid(ctypes, 0x3628E81B, 0x3CAC, 0x4C60,
                         (0xB7, 0xF4, 0x23, 0xCE, 0x0E, 0x0C, 0x33, 0x56)),
        "item": _guid(ctypes, 0x79C3F95B, 0x31F7, 0x4EC2,
                      (0xA4, 0x64, 0x63, 0x2E, 0xF5, 0xD3, 0x07, 0x60)),
        "poolstat2": _guid(ctypes, 0x589B103F, 0x6BBC, 0x5DF5,
                           (0xA9, 0x91, 0x02, 0xE2, 0x8B, 0x3B, 0x66, 0xD5)),
        "dxgidev": _guid(ctypes, 0x54EC77FA, 0x1377, 0x44E6,
                         (0x8C, 0x32, 0x88, 0xFD, 0x5F, 0x44, 0xC8, 0x4C)),
        "d3ddev": _guid(ctypes, 0xA37624AB, 0x8D5F, 0x4650,
                        (0x9D, 0x3E, 0x9E, 0xAE, 0x3D, 0x9B, 0xC6, 0x70)),
        "ifaceaccess": _guid(ctypes, 0xA9B3D012, 0x3DF2, 0x4EE3,
                             (0xB8, 0xD1, 0x86, 0x95, 0xF4, 0x57, 0xD3, 0xC1)),
        "tex2d": _guid(ctypes, 0x6F15AAF2, 0xD208, 0x4E89,
                       (0x9A, 0xB4, 0x48, 0x95, 0x35, 0xD3, 0x4F, 0x9C)),
        "sess2": _guid(ctypes, 0x2C39AE40, 0x7D2E, 0x5044,
                       (0x80, 0x4E, 0x8B, 0x67, 0x99, 0xD4, 0xCF, 0x9E)),
        "sess3": _guid(ctypes, 0xF2CDD966, 0x22AE, 0x5EA1,
                       (0x95, 0x96, 0x3A, 0x28, 0x93, 0x44, 0xC3, 0xBE)),
    }


def _structs(ctypes):
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
        _fields_ = [("pData", ctypes.c_void_p), ("RowPitch", ctypes.c_uint32),
                    ("DepthPitch", ctypes.c_uint32)]

    return SizeInt32, TEX2D_DESC, MAPPED


def _hstring(ctypes, combase, s):
    from ctypes import c_void_p, byref, POINTER
    h = c_void_p()
    combase.WindowsCreateString.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32,
                                            POINTER(c_void_p)]
    combase.WindowsCreateString(s, len(s), byref(h))
    return h


def _open_session(hwnd):
    """Set up WGC for a window and return a session dict (or None). Reusable: the
    caller pulls frames with _grab(sess) and tears down with _close(sess)."""
    if not _IS_WIN or not hwnd:
        return None
    import ctypes
    from ctypes import wintypes, byref, c_void_p, POINTER
    c_long = ctypes.c_long
    combase = ctypes.WinDLL("combase.dll")
    d3d11 = ctypes.WinDLL("d3d11.dll")
    iid = _iids(ctypes)
    SizeInt32, TEX2D_DESC, MAPPED = _structs(ctypes)

    s = {"ctypes": ctypes, "c_long": c_long, "MAPPED": MAPPED, "ro": False,
         "device": c_void_p(), "context": c_void_p(), "d3ddev": c_void_p(),
         "item": c_void_p(), "pool": c_void_p(), "session": c_void_p(),
         "staging": c_void_p(), "iid": iid, "w": 0, "h": 0}
    try:
        combase.RoInitialize.restype = c_long
        s["ro"] = (combase.RoInitialize(_RO_INIT_MULTITHREADED) >= 0)

        d3d11.D3D11CreateDevice.restype = c_long
        d3d11.D3D11CreateDevice.argtypes = [c_void_p, ctypes.c_int, c_void_p,
            ctypes.c_uint, c_void_p, ctypes.c_uint, ctypes.c_uint,
            POINTER(c_void_p), c_void_p, POINTER(c_void_p)]
        if d3d11.D3D11CreateDevice(None, _D3D_DRIVER_HARDWARE, None,
                _D3D11_CREATE_BGRA_SUPPORT, None, 0, _D3D11_SDK_VERSION,
                byref(s["device"]), None, byref(s["context"])) < 0:
            return _close(s) or None

        dxgi = c_void_p()
        if _method(ctypes, s["device"], 0, c_long, POINTER(type(iid["dxgidev"])),
                   POINTER(c_void_p))(byref(iid["dxgidev"]), byref(dxgi)) < 0:
            return _close(s) or None
        d3d11.CreateDirect3D11DeviceFromDXGIDevice.restype = c_long
        d3d11.CreateDirect3D11DeviceFromDXGIDevice.argtypes = [c_void_p, POINTER(c_void_p)]
        insp = c_void_p()
        if d3d11.CreateDirect3D11DeviceFromDXGIDevice(dxgi, byref(insp)) < 0:
            return _close(s) or None
        _method(ctypes, dxgi, 2, ctypes.c_ulong)()
        if _method(ctypes, insp, 0, c_long, POINTER(type(iid["d3ddev"])),
                   POINTER(c_void_p))(byref(iid["d3ddev"]), byref(s["d3ddev"])) < 0:
            return _close(s) or None
        _method(ctypes, insp, 2, ctypes.c_ulong)()

        interop = c_void_p()
        combase.RoGetActivationFactory.restype = c_long
        combase.RoGetActivationFactory.argtypes = [c_void_p, c_void_p, POINTER(c_void_p)]
        if combase.RoGetActivationFactory(
                _hstring(ctypes, combase, "Windows.Graphics.Capture.GraphicsCaptureItem"),
                byref(iid["interop"]), byref(interop)) < 0:
            return _close(s) or None
        if _method(ctypes, interop, 3, c_long, wintypes.HWND, POINTER(type(iid["item"])),
                   POINTER(c_void_p))(hwnd, byref(iid["item"]), byref(s["item"])) < 0 \
                or not s["item"]:
            return _close(s) or None
        _method(ctypes, interop, 2, ctypes.c_ulong)()

        size = SizeInt32()
        if _method(ctypes, s["item"], 7, c_long, POINTER(SizeInt32))(byref(size)) < 0:
            return _close(s) or None
        s["w"], s["h"] = size.W, size.H
        if s["w"] <= 0 or s["h"] <= 0:
            return _close(s) or None

        pstat = c_void_p()
        if combase.RoGetActivationFactory(
                _hstring(ctypes, combase, "Windows.Graphics.Capture.Direct3D11CaptureFramePool"),
                byref(iid["poolstat2"]), byref(pstat)) < 0 or not pstat:
            return _close(s) or None
        if _method(ctypes, pstat, 6, c_long, c_void_p, ctypes.c_int, ctypes.c_int32,
                   SizeInt32, POINTER(c_void_p))(
                   s["d3ddev"], _PIXFMT_BGRA8, 2, size, byref(s["pool"])) < 0 \
                or not s["pool"]:
            return _close(s) or None
        _method(ctypes, pstat, 2, ctypes.c_ulong)()

        if _method(ctypes, s["pool"], 10, c_long, c_void_p, POINTER(c_void_p))(
                s["item"], byref(s["session"])) < 0 or not s["session"]:
            return _close(s) or None
        # kill the yellow border + cursor (best-effort; newer Windows only)
        for _iid, _slot in ((iid["sess2"], 7), (iid["sess3"], 7)):
            try:
                _ss = c_void_p()
                if _method(ctypes, s["session"], 0, c_long, POINTER(type(_iid)),
                           POINTER(c_void_p))(byref(_iid), byref(_ss)) >= 0 and _ss.value:
                    _method(ctypes, _ss, _slot, c_long, ctypes.c_byte)(0)
                    _method(ctypes, _ss, 2, ctypes.c_ulong)()
            except Exception:
                pass

        # a persistent CPU-readable staging texture we CopyResource into every frame
        desc = TEX2D_DESC(s["w"], s["h"], 1, 1, _DXGI_B8G8R8A8_UNORM, 1, 0,
                          _D3D11_USAGE_STAGING, 0, _D3D11_CPU_ACCESS_READ, 0)
        if _method(ctypes, s["device"], 5, c_long, POINTER(TEX2D_DESC), c_void_p,
                   POINTER(c_void_p))(byref(desc), None, byref(s["staging"])) < 0 \
                or not s["staging"]:
            return _close(s) or None

        _method(ctypes, s["session"], 6, c_long)()   # StartCapture
        return s
    except Exception:
        _close(s)
        return None


def _grab(sess, timeout_s=0.25):
    """Pull the next WGC frame into the session's staging texture and return tight
    BGRA bytes (row padding stripped), or None if none arrived within timeout_s."""
    ctypes = sess["ctypes"]
    from ctypes import byref, c_void_p, POINTER
    c_long = sess["c_long"]
    iid = sess["iid"]
    frame = c_void_p()
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        frame = c_void_p()
        _method(ctypes, sess["pool"], 7, c_long, POINTER(c_void_p))(byref(frame))
        if frame.value:
            break
        time.sleep(0.002)
    if not frame or not frame.value:
        return None
    tex = c_void_p()
    surface = c_void_p()
    access = c_void_p()
    mapped = False
    try:
        if _method(ctypes, frame, 6, c_long, POINTER(c_void_p))(byref(surface)) < 0:
            return None
        if _method(ctypes, surface, 0, c_long, POINTER(type(iid["ifaceaccess"])),
                   POINTER(c_void_p))(byref(iid["ifaceaccess"]), byref(access)) < 0:
            return None
        if _method(ctypes, access, 3, c_long, POINTER(type(iid["tex2d"])),
                   POINTER(c_void_p))(byref(iid["tex2d"]), byref(tex)) < 0 or not tex:
            return None
        _method(ctypes, sess["context"], 47, None, c_void_p, c_void_p)(sess["staging"], tex)
        m = sess["MAPPED"]()
        if _method(ctypes, sess["context"], 14, c_long, c_void_p, ctypes.c_uint,
                   ctypes.c_int, ctypes.c_uint, POINTER(sess["MAPPED"]))(
                   sess["staging"], 0, _D3D11_MAP_READ, 0, byref(m)) < 0:
            return None
        mapped = True
        rowbytes = sess["w"] * 4
        base, pitch = m.pData, m.RowPitch
        if pitch == rowbytes:
            data = ctypes.string_at(base, rowbytes * sess["h"])
        else:
            data = b"".join(ctypes.string_at(base + y * pitch, rowbytes)
                            for y in range(sess["h"]))
        return data
    except Exception:
        return None
    finally:
        if mapped:
            try:
                _method(ctypes, sess["context"], 15, None, c_void_p, ctypes.c_uint)(
                    sess["staging"], 0)
            except Exception:
                pass
        for p in (tex, access, surface, frame):
            try:
                if p and p.value:
                    _method(ctypes, p, 2, ctypes.c_ulong)()
            except Exception:
                pass


def _close(sess):
    if not sess:
        return None
    ctypes = sess.get("ctypes")
    if ctypes is None:
        return None
    for key in ("staging", "session", "pool", "item", "d3ddev", "context", "device"):
        p = sess.get(key)
        try:
            if p and p.value:
                _method(ctypes, p, 2, ctypes.c_ulong)()
        except Exception:
            pass
    if sess.get("ro"):
        try:
            ctypes.WinDLL("combase.dll").RoUninitialize()
        except Exception:
            pass
    return None


def grab_frame(hwnd, timeout_s=2.0):
    """Single-frame capture -> (w, h, bgra_bytes) or None. Validation / test-button use."""
    sess = _open_session(hwnd)
    if not sess:
        return None
    try:
        data = _grab(sess, timeout_s=timeout_s)
        if data is None:
            return None
        return (sess["w"], sess["h"], data)
    finally:
        _close(sess)


def capture_clip(hwnd, out_path, seconds, fps=30, ffmpeg="ffmpeg", encoder="h264_nvenc",
                 extra_out=None, should_stop=None, state=None):
    """Continuously capture ONLY the window via WGC and encode to out_path with ffmpeg
    (NVENC). Feeds raw BGRA frames to ffmpeg's stdin at a steady `fps` (reusing the last
    frame when the window hasn't produced a new one, so timing stays even). Returns a
    stats dict {ok, frames, seconds, avg_grab_ms, mode} or {ok: False}. This is the
    recorder primitive; the game-only recording path in fragroute_capture drives it.
    Any failure -> ok False so the caller can fall back to ddagrab."""
    if not _IS_WIN or not hwnd:
        return {"ok": False, "reason": "no hwnd / not windows"}
    sess = _open_session(hwnd)
    if not sess:
        return {"ok": False, "reason": "wgc session open failed"}
    w, h = sess["w"], sess["h"]
    proc = None
    frames = 0
    grab_ms_total = 0.0
    try:
        args = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                "-f", "rawvideo", "-pixel_format", "bgra",
                "-video_size", "%dx%d" % (w, h), "-framerate", str(int(fps)),
                "-i", "pipe:0",
                "-c:v", encoder, "-pix_fmt", "yuv420p"]
        # NVENC quality/preset that stays cheap on the GPU; generic fallback otherwise.
        if "nvenc" in encoder:
            args += ["-preset", "p4", "-tune", "ll", "-rc", "vbr", "-cq", "23", "-b:v", "0"]
        else:
            args += ["-preset", "veryfast", "-crf", "23"]
        args += (extra_out or []) + [out_path]
        proc = subprocess.Popen(args, stdin=subprocess.PIPE,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                creationflags=_NO_WINDOW if _IS_WIN else 0)
        try:
            import fragroute_proc as _fp
            _fp.adopt(proc)     # never orphan the encoder
        except Exception:
            pass
        if isinstance(state, dict):
            state.update(mode="wgc", w=w, h=h, fps=int(fps))

        interval = 1.0 / float(fps)
        t0 = time.time()
        next_t = t0
        last = None
        blank = b"\x00" * (w * h * 4)
        while True:
            if should_stop is not None and should_stop():
                break
            if seconds and (time.time() - t0) >= seconds:
                break
            g0 = time.time()
            data = _grab(sess, timeout_s=interval)
            grab_ms_total += (time.time() - g0) * 1000.0
            if data is not None:
                last = data
            try:
                proc.stdin.write(last if last is not None else blank)
                frames += 1
            except (BrokenPipeError, OSError):
                break
            next_t += interval
            slp = next_t - time.time()
            if slp > 0:
                time.sleep(slp)
            else:
                next_t = time.time()   # we fell behind; don't spiral
        try:
            proc.stdin.close()
        except Exception:
            pass
        try:
            proc.wait(timeout=15)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        dur = max(1e-6, time.time() - t0)
        ok = os.path.exists(out_path) and os.path.getsize(out_path) > 0
        return {"ok": ok, "frames": frames, "seconds": round(dur, 2),
                "avg_grab_ms": round(grab_ms_total / max(1, frames), 2),
                "eff_fps": round(frames / dur, 1), "mode": "wgc", "w": w, "h": h}
    except Exception as e:
        try:
            if proc:
                proc.kill()
        except Exception:
            pass
        return {"ok": False, "reason": str(e)[:140]}
    finally:
        _close(sess)
