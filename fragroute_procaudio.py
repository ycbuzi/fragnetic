"""Per-process WASAPI loopback -- record ONLY FragPunk's audio, not the whole desktop.

The normal recorder (fragroute_audio) captures the DEFAULT OUTPUT device's loopback,
which is a mix of EVERY app (Discord, browser, music...). Users reported their clips
carrying that other audio. Windows 10 2004+ exposes a *process* loopback: activate an
IAudioClient on the magic "VAD\\Process_Loopback" endpoint with
AUDIOCLIENT_ACTIVATION_PARAMS targeting the game's PID (INCLUDE_TARGET_PROCESS_TREE),
and you get a render stream of just that process tree. pyaudiowpatch can't do this, so
this is raw ctypes COM, modelled on Microsoft's ApplicationLoopback sample.

Public API:
  available()            -> bool  (Windows + the activation API loads)
  find_fragpunk_pids()   -> [int] (the game's audio process; shipping-exe preferred)
  capture(pids, wav_path, should_stop, state) -> bool
        Blocks, writing a growing 16-bit PCM WAV (same contract as fragroute_audio's
        device loop) until should_stop() is true. Returns True if it actually started
        capturing (caller keeps the clip); False if it could not START (caller falls
        back to whole-desktop capture). Once started it owns the WAV until stop.

Everything is wrapped so ANY failure degrades to the existing system-audio path -- the
recorder can never be broken by this, only upgraded.  Windows-only; inert elsewhere.
"""
import os
import subprocess
import time
import wave

_IS_WIN = os.name == "nt"
_NO_WINDOW = 0x08000000

# ----- Win32 / WASAPI constants -------------------------------------------------
_VIRTUAL_LOOPBACK_PATH = "VAD\\Process_Loopback"
_ACTIVATION_TYPE_PROCESS_LOOPBACK = 1
_LOOPBACK_MODE_INCLUDE_TREE = 0
_VT_BLOB = 0x0041
_SHARE_SHARED = 0
_STREAMFLAGS_LOOPBACK = 0x00020000
_STREAMFLAGS_EVENTCALLBACK = 0x00040000
_STREAMFLAGS_AUTOCONVERTPCM = 0x80000000
_STREAMFLAGS_SRC_DEFAULT_QUALITY = 0x08000000
_BUFFERFLAGS_SILENT = 0x2
_WAVE_FORMAT_PCM = 1
_WAIT_OBJECT_0 = 0x0
_INFINITE = 0xFFFFFFFF
_S_OK = 0
_E_NOINTERFACE = -2147467262      # 0x80004002
_COINIT_MULTITHREADED = 0x0

_GAME_NAMES = ("fragpunk-win64-shipping.exe", "fragpunk.exe", "fragpunk_launcher.exe")


def find_fragpunk_pids():
    """PIDs of the running FragPunk audio process(es). The Unreal 'shipping' exe is
    what actually plays sound, so it's preferred; we still return the others so the
    process-tree capture picks up audio no matter which node owns the render stream."""
    if not _IS_WIN:
        return []
    shipping, generic = [], []
    try:
        out = subprocess.run(["tasklist", "/fo", "csv", "/nh"],
                             capture_output=True, text=True, timeout=6,
                             creationflags=_NO_WINDOW).stdout
        for line in out.splitlines():
            cols = [c.strip('" ') for c in line.split('","')]
            if len(cols) < 2 or not cols[1].isdigit():
                continue
            name = cols[0].strip('" ').lower()
            if name not in _GAME_NAMES:
                continue
            pid = int(cols[1])
            # last column is mem usage like "9,701,068 K" -- the real game (render)
            # process is the memory-heavy one; a launcher/helper is tiny.
            mem = 0
            try:
                mem = int(cols[-1].lower().replace("k", "").replace(",", "").strip())
            except Exception:
                mem = 0
            (shipping if name == "fragpunk-win64-shipping.exe" else generic).append((mem, pid))
    except Exception:
        return []
    # Prefer the explicit shipping exe, then order every candidate by memory desc so
    # the actual audio-producing game process is targeted before any tiny launcher.
    shipping.sort(reverse=True)
    generic.sort(reverse=True)
    return [pid for _, pid in shipping] + [pid for _, pid in generic]


def available():
    """True if this OS can do process loopback (Windows 10 2004+ with the activation
    export present). Cheap -- just probes that the DLLs/symbols load."""
    if not _IS_WIN:
        return False
    try:
        import ctypes
        ctypes.WinDLL("ole32.dll")
        mm = ctypes.WinDLL("Mmdevapi.dll")
        getattr(mm, "ActivateAudioInterfaceAsync")
        return True
    except Exception:
        return False


# ----- ctypes COM plumbing ------------------------------------------------------
def _guid(ctypes, d1, d2, d3, tail):
    class GUID(ctypes.Structure):
        _fields_ = [("Data1", ctypes.c_uint32), ("Data2", ctypes.c_uint16),
                    ("Data3", ctypes.c_uint16), ("Data4", ctypes.c_ubyte * 8)]
    g = GUID()
    g.Data1, g.Data2, g.Data3 = d1, d2, d3
    for i, b in enumerate(tail):
        g.Data4[i] = b
    return g, GUID


def _method(ctypes, ptr, index, restype, *argtypes):
    """Bind vtable slot `index` of the COM interface at `ptr` to a callable(this, ...)."""
    vtbl = ctypes.cast(ptr, ctypes.POINTER(ctypes.c_void_p)).contents.value
    fn_addr = ctypes.cast(vtbl, ctypes.POINTER(ctypes.c_void_p))[index]
    proto = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)
    fn = proto(fn_addr)
    return lambda *a: fn(ptr, *a)


def capture(pids, wav_path, should_stop, state):
    """Record just `pids`' audio to wav_path (16-bit PCM). See module docstring."""
    if not _IS_WIN or not pids:
        return False
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:
        return False

    ole32 = ctypes.WinDLL("ole32.dll")
    k32 = ctypes.WinDLL("kernel32.dll", use_last_error=True)
    mm = ctypes.WinDLL("Mmdevapi.dll")

    # --- IIDs ---
    IID_IAudioClient, GUID = _guid(ctypes, 0x1CB9AD4C, 0xDBFA, 0x4C32,
                                   (0xB1, 0x78, 0xC2, 0xF5, 0x68, 0xA7, 0x03, 0xB2))
    IID_IAudioCaptureClient, _ = _guid(ctypes, 0xC8ADBD64, 0xE71E, 0x48A0,
                                       (0xA4, 0xDE, 0x18, 0x5C, 0x39, 0x5C, 0xD3, 0x17))
    IID_ICompletion, _ = _guid(ctypes, 0x41D949AB, 0x9862, 0x444A,
                               (0x80, 0xF6, 0xC2, 0x61, 0x33, 0x4D, 0xA5, 0xEB))
    IID_IAgile, _ = _guid(ctypes, 0x94EA2B94, 0xE9CC, 0x49E0,
                          (0xC0, 0xFF, 0xEE, 0x64, 0xCA, 0x8F, 0x5B, 0x90))
    IID_IUnknown, _ = _guid(ctypes, 0x00000000, 0x0000, 0x0000,
                            (0xC0, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x46))

    def _iid_bytes(g):
        return ctypes.string_at(ctypes.byref(g), ctypes.sizeof(g))
    _known_iids = {_iid_bytes(IID_ICompletion), _iid_bytes(IID_IUnknown),
                   _iid_bytes(IID_IAgile)}

    # --- structs ---
    class WAVEFORMATEX(ctypes.Structure):
        _fields_ = [("wFormatTag", ctypes.c_uint16), ("nChannels", ctypes.c_uint16),
                    ("nSamplesPerSec", ctypes.c_uint32), ("nAvgBytesPerSec", ctypes.c_uint32),
                    ("nBlockAlign", ctypes.c_uint16), ("wBitsPerSample", ctypes.c_uint16),
                    ("cbSize", ctypes.c_uint16)]

    class PROC_LOOPBACK_PARAMS(ctypes.Structure):
        _fields_ = [("TargetProcessId", ctypes.c_uint32),
                    ("ProcessLoopbackMode", ctypes.c_int)]

    class ACTIVATION_PARAMS(ctypes.Structure):
        _fields_ = [("ActivationType", ctypes.c_int),
                    ("ProcessLoopbackParams", PROC_LOOPBACK_PARAMS)]

    class PROPVARIANT(ctypes.Structure):
        # 64-bit layout: vt + 3 reserved WORDs (8 bytes) then the value union.
        _fields_ = [("vt", ctypes.c_uint16), ("r1", ctypes.c_uint16),
                    ("r2", ctypes.c_uint16), ("r3", ctypes.c_uint16),
                    ("cbSize", ctypes.c_uint32), ("_pad", ctypes.c_uint32),
                    ("pBlobData", ctypes.c_void_p)]

    # --- completion handler (a hand-built COM object) ------------------------
    done_evt = k32.CreateEventW(None, True, False, None)   # manual-reset

    QI = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)
    ADDREF = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)
    ACT = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p)

    obj_this = ctypes.c_void_p()   # set below to address of the object

    def _qi(this, riid, ppv):
        try:
            want = ctypes.string_at(riid, 16)
            out = ctypes.cast(ppv, ctypes.POINTER(ctypes.c_void_p))
            if want in _known_iids:
                out[0] = obj_this
                return _S_OK
            out[0] = None
            return _E_NOINTERFACE
        except Exception:
            return _E_NOINTERFACE

    def _addref(this):
        return 1

    def _release(this):
        return 1

    def _act_completed(this, op):
        k32.SetEvent(done_evt)
        return _S_OK

    qi_c, ar_c, rl_c, act_c = QI(_qi), ADDREF(_addref), ADDREF(_release), ACT(_act_completed)
    vtbl = (ctypes.c_void_p * 4)(
        ctypes.cast(qi_c, ctypes.c_void_p), ctypes.cast(ar_c, ctypes.c_void_p),
        ctypes.cast(rl_c, ctypes.c_void_p), ctypes.cast(act_c, ctypes.c_void_p))
    obj = (ctypes.c_void_p * 1)(ctypes.cast(vtbl, ctypes.c_void_p))
    obj_this.value = ctypes.cast(obj, ctypes.c_void_p).value

    com_inited = False
    audio_client = None
    cap = None
    raw = None
    wf_out = None
    cap_evt = None
    started = False
    try:
        ole32.CoInitializeEx(None, _COINIT_MULTITHREADED)
        com_inited = True

        # Build activation params -> PROPVARIANT(VT_BLOB). We try each candidate PID;
        # the first that activates + initializes wins.
        wf = WAVEFORMATEX(_WAVE_FORMAT_PCM, 2, 48000, 48000 * 4, 4, 16, 0)
        block_align = wf.nBlockAlign
        sr, ch = wf.nSamplesPerSec, wf.nChannels

        mm.ActivateAudioInterfaceAsync.restype = ctypes.c_long
        mm.ActivateAudioInterfaceAsync.argtypes = [
            ctypes.c_wchar_p, ctypes.c_void_p, ctypes.c_void_p,
            ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]

        for pid in pids:
            params = ACTIVATION_PARAMS()
            params.ActivationType = _ACTIVATION_TYPE_PROCESS_LOOPBACK
            params.ProcessLoopbackParams.TargetProcessId = int(pid)
            params.ProcessLoopbackParams.ProcessLoopbackMode = _LOOPBACK_MODE_INCLUDE_TREE
            pv = PROPVARIANT()
            pv.vt = _VT_BLOB
            pv.cbSize = ctypes.sizeof(params)
            pv.pBlobData = ctypes.cast(ctypes.byref(params), ctypes.c_void_p)

            op = ctypes.c_void_p()
            k32.ResetEvent(done_evt)
            hr = mm.ActivateAudioInterfaceAsync(
                _VIRTUAL_LOOPBACK_PATH, ctypes.byref(IID_IAudioClient),
                ctypes.byref(pv), obj_this, ctypes.byref(op))
            if hr < 0 or not op:
                continue
            # wait for the async ActivateCompleted callback (SetEvent)
            if k32.WaitForSingleObject(done_evt, 3000) != _WAIT_OBJECT_0:
                continue
            # GetActivateResult(op, &hrActivate, &IUnknown*) -- vtable slot 3
            get_result = _method(ctypes, op, 3, ctypes.c_long,
                                 ctypes.POINTER(ctypes.c_long), ctypes.POINTER(ctypes.c_void_p))
            hr_act = ctypes.c_long(0)
            iface = ctypes.c_void_p()
            hr = get_result(ctypes.byref(hr_act), ctypes.byref(iface))
            # release the async op
            try:
                _method(ctypes, op, 2, ctypes.c_ulong)()   # Release
            except Exception:
                pass
            if hr < 0 or hr_act.value < 0 or not iface:
                continue
            audio_client = iface.value

            # IAudioClient::Initialize (slot 3)
            init = _method(ctypes, audio_client, 3, ctypes.c_long,
                          ctypes.c_int, ctypes.c_uint32, ctypes.c_longlong,
                          ctypes.c_longlong, ctypes.c_void_p, ctypes.c_void_p)
            flags = (_STREAMFLAGS_LOOPBACK | _STREAMFLAGS_EVENTCALLBACK |
                     _STREAMFLAGS_AUTOCONVERTPCM | _STREAMFLAGS_SRC_DEFAULT_QUALITY)
            hr = init(_SHARE_SHARED, flags, 2000000, 0, ctypes.byref(wf), None)
            if hr < 0:
                # can't reuse an IAudioClient after a failed Initialize; drop it
                try:
                    _method(ctypes, audio_client, 2, ctypes.c_ulong)()
                except Exception:
                    pass
                audio_client = None
                continue
            break   # activated + initialized

        if not audio_client:
            return False

        # event handle for the capture cadence
        cap_evt = k32.CreateEventW(None, False, False, None)
        _method(ctypes, audio_client, 13, ctypes.c_long, ctypes.c_void_p)(cap_evt)  # SetEventHandle

        # GetService(IID_IAudioCaptureClient) -- slot 14
        cap = ctypes.c_void_p()
        hr = _method(ctypes, audio_client, 14, ctypes.c_long,
                    ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))(
                    ctypes.byref(IID_IAudioCaptureClient), ctypes.byref(cap))
        if hr < 0 or not cap:
            return False
        cap = cap.value
        get_next = _method(ctypes, cap, 5, ctypes.c_long, ctypes.POINTER(ctypes.c_uint32))
        get_buf = _method(ctypes, cap, 3, ctypes.c_long,
                         ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_uint32),
                         ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p, ctypes.c_void_p)
        rel_buf = _method(ctypes, cap, 4, ctypes.c_long, ctypes.c_uint32)

        # open the WAV (own the handle so a rolling save can read recent PCM off disk)
        raw = open(str(wav_path), "wb")
        wf_out = wave.open(raw, "wb")
        wf_out.setnchannels(ch)
        wf_out.setsampwidth(2)
        wf_out.setframerate(sr)

        _method(ctypes, audio_client, 10, ctypes.c_long)()   # IAudioClient::Start
        started = True
        if isinstance(state, dict):
            state.update(mode="process", sr=sr, ch=ch, started=time.time(),
                         frames=0, err="", device="FragPunk (per-process)")

        import array
        frames_written = 0
        silent_run = 0
        while not should_stop():
            k32.WaitForSingleObject(cap_evt, 200)
            while True:
                pkt = ctypes.c_uint32(0)
                if get_next(ctypes.byref(pkt)) < 0 or pkt.value == 0:
                    break
                pdata = ctypes.c_void_p()
                nframes = ctypes.c_uint32(0)
                dwflags = ctypes.c_uint32(0)
                if get_buf(ctypes.byref(pdata), ctypes.byref(nframes),
                           ctypes.byref(dwflags), None, None) < 0:
                    break
                nb = nframes.value * block_align
                if dwflags.value & _BUFFERFLAGS_SILENT or not pdata:
                    data = b"\x00" * nb
                    silent_run += 1
                else:
                    data = ctypes.string_at(pdata, nb)
                    silent_run = 0
                wf_out.writeframes(data)
                rel_buf(nframes.value)
                frames_written += 1
                if (frames_written & 7) == 0:
                    if isinstance(state, dict):
                        state["frames"] = frames_written
                        try:
                            state["level"] = _rms16(data)
                        except Exception:
                            pass
                    try:
                        raw.flush()
                    except Exception:
                        pass

        # graceful stop; all handle teardown happens in finally (single path)
        try:
            _method(ctypes, audio_client, 11, ctypes.c_long)()   # Stop
        except Exception:
            pass
        return True

    except Exception as e:
        if isinstance(state, dict):
            state["err_proc"] = str(e)[:140]
        # if we never got the stream Start'd, tell the caller to fall back
        return started
    finally:
        # close the WAV/file handle FIRST so a caller falling back to the device
        # loop can reopen the same path without a sharing violation.
        try:
            if wf_out is not None:
                wf_out.close()
        except Exception:
            pass
        try:
            if raw is not None and not raw.closed:
                raw.close()
        except Exception:
            pass
        try:
            if cap_evt:
                k32.CloseHandle(cap_evt)
        except Exception:
            pass
        try:
            if cap:
                _method(ctypes, cap, 2, ctypes.c_ulong)()            # release capture client
        except Exception:
            pass
        try:
            if audio_client:
                _method(ctypes, audio_client, 2, ctypes.c_ulong)()   # Release IAudioClient
        except Exception:
            pass
        try:
            if done_evt:
                k32.CloseHandle(done_evt)
        except Exception:
            pass
        if com_inited:
            try:
                ole32.CoUninitialize()
            except Exception:
                pass


def _rms16(data):
    import array
    import math
    a = array.array("h")
    try:
        a.frombytes(data)
    except Exception:
        return 0.0
    if not a:
        return 0.0
    step = max(1, len(a) // 1024)
    acc = 0.0
    n = 0
    for i in range(0, len(a), step):
        acc += a[i] * a[i]
        n += 1
    return (acc / n) ** 0.5 / 32768.0 if n else 0.0
