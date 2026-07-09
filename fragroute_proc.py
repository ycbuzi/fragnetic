"""Shared orphan-proofing for FRAGROUTE's long-lived helper subprocesses.

Windows does NOT kill a child process when its parent dies. This app is UAC-elevated
and almost always closed via the tray, a taskkill, or a crash -- so atexit/stop()
cleanup never runs, and every helper sidecar we spawn (whisper-server, llama-server,
ffmpeg, sd-cli, piper) would orphan and keep hogging CPU/GPU long after the app is
gone. On the GPU that is a real in-game FPS drain, and the orphans pile up run over
run.

A single process-wide Job Object with JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE fixes this
at the OS level: adopt() puts a child in the job, and when THIS process dies by ANY
means (clean exit, crash, taskkill) the OS tears the job down and kills every adopted
child. Belt-and-suspenders with each module's own stop()/atexit path.

  * adopt(proc) -- put a persistent child in the kill-on-close job (call right after Popen).
  * reap(*names) -- taskkill orphans left behind by a previous pre-fix/crashed run.
  * run(...)     -- a subprocess.run drop-in that ALSO adopts the child, so a blocking
                    one-shot (GPU image-gen, a long transcode) can't orphan either.

Pure stdlib. Import-safe and inert on non-Windows: every function degrades to a plain
subprocess call / no-op. Extracted from fragroute_voice.py's proven whisper fix so the
LLM, TTS, image-gen, video and capture sidecars share the exact same protection.
"""
import os
import subprocess

# One job for the whole process. The handle is kept alive for the life of the
# interpreter (closing it is what triggers KILL_ON_JOB_CLOSE), so we cache it.
_JOB = {"handle": None, "tried": False}
_NOWIN = {"creationflags": 0x08000000} if os.name == "nt" else {}  # CREATE_NO_WINDOW


def _kill_on_close_job():
    """Return a cached Job Object handle whose members die when this process dies.
    None if unavailable (non-Windows / API failure) -- callers degrade gracefully."""
    if os.name != "nt":
        return None
    if _JOB["tried"]:
        return _JOB["handle"]
    _JOB["tried"] = True
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateJobObjectW.restype = wintypes.HANDLE
        k32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
        job = k32.CreateJobObjectW(None, None)
        if not job:
            return None

        class _BASIC(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64),
                        ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", wintypes.DWORD),
                        ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t),
                        ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t),
                        ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class _IOC(ctypes.Structure):
            _fields_ = [("ReadOperationCount", ctypes.c_uint64),
                        ("WriteOperationCount", ctypes.c_uint64),
                        ("OtherOperationCount", ctypes.c_uint64),
                        ("ReadTransferCount", ctypes.c_uint64),
                        ("WriteTransferCount", ctypes.c_uint64),
                        ("OtherTransferCount", ctypes.c_uint64)]

        class _EXT(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", _BASIC),
                        ("IoInfo", _IOC),
                        ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t),
                        ("PeakJobMemoryUsed", ctypes.c_size_t)]

        info = _EXT()
        info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        k32.SetInformationJobObject.restype = wintypes.BOOL
        k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                                wintypes.LPVOID, wintypes.DWORD]
        if not k32.SetInformationJobObject(job, 9,  # JobObjectExtendedLimitInformation
                                           ctypes.byref(info), ctypes.sizeof(info)):
            return None
        _JOB["handle"] = job   # keep the handle alive for the life of the process
        return job
    except Exception:
        return None


def adopt(proc):
    """Put a child process in the kill-on-close job so it can never orphan.
    Call right after subprocess.Popen() of any persistent helper. No-op on
    non-Windows or if the job API is unavailable -- callers degrade gracefully."""
    if os.name != "nt" or proc is None:
        return
    try:
        import ctypes
        from ctypes import wintypes
        job = _kill_on_close_job()
        if not job:
            return
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.AssignProcessToJobObject.restype = wintypes.BOOL
        k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        ok = k32.AssignProcessToJobObject(job, int(proc._handle))
        if not ok:
            # Silent failure defeats orphan-prevention. Common cause: the process is
            # already in a job that forbids nesting/breakaway. Try to breakaway-and-adopt;
            # if that also fails, the startup reap() is the backstop. Return the result so
            # it's observable rather than a silent no-op.
            err = ctypes.get_last_error()
            return {"ok": False, "err": err}
        return {"ok": True}
    except Exception:
        return {"ok": False, "err": "exc"}
    return {"ok": True}


def reap(*image_names):
    """Kill helper exes left behind by a previous crashed/killed run so they don't
    accumulate (a pre-fix build could leave several). Safe for this single-user app --
    callers only invoke this when they don't currently own a live server, so this only
    kills stale ones; the servers we spawn next are adopt()'d and can never orphan again."""
    if os.name != "nt":
        return
    for name in image_names:
        if not name:
            continue
        try:
            subprocess.run(["taskkill", "/IM", name, "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=10, **_NOWIN)
        except Exception:
            pass


def run(args, timeout=None, capture_output=False, text=False, input=None, **popen_kw):
    """subprocess.run work-alike that ALSO job-adopts the child, so a blocking
    one-shot (GPU image-gen, a long ffmpeg transcode) can't orphan if we're hard-killed
    mid-run. Returns a subprocess.CompletedProcess and raises TimeoutExpired just like
    subprocess.run(). Extra kwargs (creationflags, startupinfo, cwd, ...) flow to Popen."""
    if capture_output:
        popen_kw["stdout"] = subprocess.PIPE
        popen_kw["stderr"] = subprocess.PIPE
    if input is not None and "stdin" not in popen_kw:
        popen_kw["stdin"] = subprocess.PIPE
    if text and "errors" not in popen_kw:
        # A helper's output (sd-cli, ffmpeg, ...) can contain bytes the locale codec (cp1252)
        # can't decode; with strict errors the subprocess READER THREAD dies with
        # UnicodeDecodeError and the captured output is lost. Default to replacement.
        popen_kw["errors"] = "replace"
    proc = subprocess.Popen(args, text=text, **popen_kw)
    adopt(proc)   # OS kills it if we die by any means, even though this call blocks
    try:
        out, err = proc.communicate(input=input, timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            out, err = proc.communicate()
        except Exception:
            pass
        raise
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
        raise
    return subprocess.CompletedProcess(args, proc.returncode, out, err)
