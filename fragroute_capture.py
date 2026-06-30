"""FRAGROUTE capture engine -- low-impact match recording for AI review.

Goal (see memory: fragroute-ai-coach): record gameplay with ~zero FPS cost so
the AI can review it BETWEEN matches (and later, live on the dedicated GPU).

How it stays out of your game's way:
  * DESKTOP-DUPLICATION capture (ffmpeg `ddagrab`, DXGI) -- captures at the
    Windows compositor level, NEVER injects into the game. Anti-cheat safe,
    unlike OBS "Game Capture" hooks.
  * NVENC hardware encoding -- the GPU's dedicated encoder block, separate
    silicon from the rendering shaders, so compressing video doesn't steal
    frames. On this rig the encode can be pinned to the GTX 1650 SUPER (GPU
    idx 1) so the RTX 4070 SUPER (idx 0) stays 100% on the game.
  * REPLAY-BUFFER style: ffmpeg continuously writes short segments into a small
    ring (a few minutes, auto-overwritten). Saving a clip just concatenates the
    most recent segments -- no constant full-disk recording, no re-encode.

ffmpeg is an external binary (like the bundled wireguard.exe). It is NOT packed
into the --onefile exe (an ~80 MB payload would slow every elevated launch);
instead it lives next to the exe / in dist/ and we discover it at runtime. If
it's missing, every call degrades gracefully with a clear "needs ffmpeg" status.

Pure stdlib. The engine passes a base directory; nothing here imports fragroute.
"""
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

APP_CAPTURE_BUILD = "cap-1"

# Module state (guarded by _LOCK)
_LOCK = threading.Lock()
_STATE = {
    "proc": None,          # ffmpeg subprocess.Popen while recording
    "ring_dir": None,      # Path to the segment ring
    "started": 0.0,        # epoch seconds recording began
    "settings": {},        # last start() options
    "ffmpeg": None,        # cached discovered ffmpeg path
    "ffmpeg_checked": False,
    "probe": None,         # cached capability probe
}

# --- tunables (overridable via start() opts) -------------------------------
SEG_SECONDS = 10           # length of each ring segment
RING_SEGMENTS = 18         # ~3 minutes of rolling buffer (18 * 10s)
DEFAULT_FPS = 30          # 30 is plenty for review footage; halves capture/encode load
DEFAULT_BITRATE = "8M"    # ample for 1080p review; lighter than 12M
DEFAULT_ENCODER = "h264_nvenc"
DEFAULT_GPU = None        # CRITICAL: do NOT pin to the 1650S. ddagrab captures on the
                           # 4070 (the display GPU); encoding on a different GPU forces
                           # a per-frame 4070->1650S PCIe copy that STUTTERS the game.
                           # None lets NVENC encode on the capture GPU (4070) zero-copy --
                           # its encoder block is separate from rendering, ~0 FPS cost.


# ===========================================================================
#  ffmpeg discovery + capability probe
# ===========================================================================
def _exe_dir():
    # When frozen by PyInstaller, sys.executable is the app exe.
    import sys
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).parent


def find_ffmpeg(refresh=False):
    """Locate ffmpeg.exe: next to the app, in dist/, or on PATH. Cached."""
    if _STATE["ffmpeg_checked"] and not refresh:
        return _STATE["ffmpeg"]
    cand = []
    d = _exe_dir()
    cand += [d / "ffmpeg.exe", d / "dist" / "ffmpeg.exe", d.parent / "ffmpeg.exe"]
    found = None
    for c in cand:
        try:
            if c.exists():
                found = str(c)
                break
        except Exception:
            pass
    if not found:
        found = shutil.which("ffmpeg")
    _STATE["ffmpeg"] = found
    _STATE["ffmpeg_checked"] = True
    return found


def _run(args, timeout=8):
    """Run ffmpeg with no window; return (rc, stdout+stderr text)."""
    flags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
    try:
        p = subprocess.run(args, capture_output=True, text=True,
                           timeout=timeout, creationflags=flags)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except Exception as e:
        return -1, str(e)


def probe(refresh=False):
    """Check what this ffmpeg can do: NVENC encoders + ddagrab capture filter."""
    if _STATE["probe"] is not None and not refresh:
        return _STATE["probe"]
    ff = find_ffmpeg(refresh=refresh)
    out = {"ffmpeg": ff, "ok": False, "nvenc": [], "ddagrab": False, "message": ""}
    if not ff:
        out["message"] = ("ffmpeg not found. Drop ffmpeg.exe next to FRAGROUTE.exe "
                          "(a build with NVENC + ddagrab) to enable recording.")
        _STATE["probe"] = out
        return out
    rc, enc = _run([ff, "-hide_banner", "-encoders"])
    for name in ("h264_nvenc", "hevc_nvenc", "av1_nvenc"):
        if name in enc:
            out["nvenc"].append(name)
    rc2, filt = _run([ff, "-hide_banner", "-filters"])
    out["ddagrab"] = ("ddagrab" in filt)
    if out["nvenc"] and out["ddagrab"]:
        out["ok"] = True
        out["message"] = "Ready: NVENC + desktop-duplication capture available."
    elif not out["nvenc"]:
        out["message"] = "This ffmpeg has no NVENC encoder; need an nvenc-enabled build."
    elif not out["ddagrab"]:
        out["message"] = "This ffmpeg lacks the ddagrab filter; need ffmpeg 6.0+ (full build)."
    _STATE["probe"] = out
    return out


# ===========================================================================
#  Recording (replay-buffer ring)
# ===========================================================================
_AUDIO_CACHE = {"checked": False, "device": None, "devices": []}


def list_audio_devices(refresh=False):
    """All dshow audio input device names (cached)."""
    if _AUDIO_CACHE["checked"] and not refresh:
        return _AUDIO_CACHE["devices"]
    ff = find_ffmpeg()
    devs = []
    if ff:
        try:
            p = subprocess.run([ff, "-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy"],
                               capture_output=True, text=True, timeout=15,
                               creationflags=(0x08000000 if os.name == "nt" else 0))
            import re
            for m in re.finditer(r'"([^"]+)"\s*\(audio\)', p.stderr or ""):
                devs.append(m.group(1))
        except Exception:
            pass
    _AUDIO_CACHE.update(checked=True, devices=devs)
    return devs


def detect_audio_loopback(refresh=False):
    """The best device for capturing GAME/desktop audio: a system loopback (Stereo
    Mix / 'What U Hear' / loopback). Returns the device name or None. We do NOT pick
    a microphone here -- a mic would record your voice/room, not the game."""
    if _AUDIO_CACHE["checked"] and not refresh and _AUDIO_CACHE["device"] is not None:
        return _AUDIO_CACHE["device"]
    dev = None
    for d in list_audio_devices(refresh):
        low = d.lower()
        if any(k in low for k in ("stereo mix", "what u hear", "loopback", "wave out", "wasapi")):
            dev = d
            break
    _AUDIO_CACHE["device"] = dev
    return dev


def _build_capture_cmd(ff, ring_dir, fps, bitrate, encoder, gpu, seg_seconds, ring_segments,
                       audio_device=None):
    """ffmpeg command: DXGI desktop-duplication -> NVENC -> mpegts segment ring.
    Optionally captures system audio from `audio_device` (a dshow loopback) so clips
    have game sound; the recorder is otherwise video-only (ddagrab has no audio).

    ddagrab auto-creates a D3D11 device and outputs GPU frames; *_nvenc accepts
    those D3D11 frames directly (zero-copy GPU encode). The segment muxer with
    -segment_wrap keeps only the most recent N files (the rolling buffer).
    """
    cmd = [ff, "-hide_banner", "-loglevel", "warning", "-nostdin", "-y"]
    if audio_device:
        cmd += ["-f", "dshow", "-i", "audio=" + audio_device]   # input #0 = system audio
    # p5 preset = NVENC default/balanced (light on the encoder); 'hq' tune for recording.
    cmd += ["-filter_complex", "ddagrab=output_idx=0:framerate=%d%s" % (int(fps), "[v]" if audio_device else "")]
    if audio_device:
        cmd += ["-map", "[v]", "-map", "0:a"]
    cmd += ["-c:v", encoder, "-preset", "p5", "-tune", "hq", "-b:v", str(bitrate)]
    if gpu is not None:
        cmd += ["-gpu", str(int(gpu))]          # only if explicitly pinned (default None)
    if audio_device:
        cmd += ["-c:a", "aac", "-b:a", "160k"]
    cmd += ["-f", "segment",
            "-segment_time", str(int(seg_seconds)),
            "-segment_wrap", str(int(ring_segments)),
            "-segment_format", "mpegts",
            "-reset_timestamps", "1",
            str(Path(ring_dir) / "seg_%03d.ts")]
    return cmd


def is_recording():
    p = _STATE["proc"]
    return p is not None and (p.poll() is None)


def start(base_dir, opts=None):
    """Begin the rolling capture. base_dir holds ring/ and clips/ subfolders."""
    opts = opts or {}
    with _LOCK:
        if is_recording():
            return {"ok": True, "already": True, "message": "Already recording."}
        pr = probe()
        if not pr["ok"]:
            return {"ok": False, "message": pr["message"]}
        ff = pr["ffmpeg"]
        ring = Path(base_dir) / "ring"
        ring.mkdir(parents=True, exist_ok=True)
        # clear any stale segments so the buffer starts clean
        for f in ring.glob("seg_*.ts"):
            try:
                f.unlink()
            except Exception:
                pass
        fps = int(opts.get("fps", DEFAULT_FPS))
        bitrate = opts.get("bitrate", DEFAULT_BITRATE)
        encoder = opts.get("encoder", DEFAULT_ENCODER)
        if encoder not in (pr["nvenc"] or []):
            encoder = (pr["nvenc"] or ["h264_nvenc"])[0]
        gpu = opts.get("gpu", DEFAULT_GPU)
        seg = int(opts.get("seg_seconds", SEG_SECONDS))
        nseg = int(opts.get("ring_segments", RING_SEGMENTS))
        # AUDIO: capture system audio (game sound) from a loopback if available + enabled.
        # Default ON; auto-detects Stereo Mix / loopback. Falls back to video-only if the
        # device fails (so a bad audio device never breaks recording).
        audio_device = None
        if opts.get("record_audio", True):
            audio_device = opts.get("audio_device") or detect_audio_loopback()
        flags = 0x08000000 if os.name == "nt" else 0

        def _launch(adev):
            cmd = _build_capture_cmd(ff, ring, fps, bitrate, encoder, gpu, seg, nseg, audio_device=adev)
            return subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, creationflags=flags)
        try:
            # stdin=DEVNULL (+ -nostdin in the cmd): a PIPE stdin makes ffmpeg quit
            # on a spurious EOF/keypress -- that was killing the recorder after ~6s.
            proc = _launch(audio_device)
        except Exception as e:
            return {"ok": False, "message": "Failed to start ffmpeg: %s" % e}
        # if audio was requested, make sure it actually started; a bad/busy audio
        # device makes ffmpeg exit fast -> retry video-only so recording still works.
        if audio_device:
            time.sleep(1.3)
            if proc.poll() is not None:
                audio_device = None
                try:
                    proc = _launch(None)
                except Exception as e:
                    return {"ok": False, "message": "Failed to start ffmpeg: %s" % e}
        _STATE["audio_device"] = audio_device
        _STATE["proc"] = proc
        _STATE["ring_dir"] = ring
        _STATE["started"] = time.time()
        _STATE["settings"] = {"fps": fps, "bitrate": bitrate, "encoder": encoder,
                              "gpu": gpu, "seg_seconds": seg, "ring_segments": nseg,
                              "buffer_seconds": seg * nseg, "audio": audio_device}
        amsg = (" + audio (%s)" % audio_device) if audio_device else " (video only)"
        # nseg == 0 => ffmpeg segment_wrap 0 => UNLIMITED segments = full-match record
        # (no rolling overwrite); otherwise it's the rolling N-second highlight buffer.
        mode = "full match" if nseg == 0 else ("rolling %ds buffer" % (seg * nseg))
        return {"ok": True, "message": "Recording (%s)%s." % (mode, amsg),
                "settings": _STATE["settings"]}


def stop():
    with _LOCK:
        p = _STATE["proc"]
        if p is None:
            return {"ok": True, "message": "Not recording."}
        try:
            if p.poll() is None:
                p.terminate()
                try:
                    p.wait(timeout=4)
                except Exception:
                    p.kill()
        except Exception:
            pass
        _STATE["proc"] = None
        return {"ok": True, "message": "Recording stopped."}


def _recent_segments(ring_dir, seconds, seg_seconds):
    """Return the most-recently-modified segment files covering ~`seconds`."""
    segs = sorted(Path(ring_dir).glob("seg_*.ts"), key=lambda f: f.stat().st_mtime)
    if not segs:
        return []
    need = max(1, int(round(seconds / float(seg_seconds))) + 1)
    return segs[-need:]


def save_clip(base_dir, seconds=30, label=None):
    """Concatenate the last ~`seconds` of the ring into clips/ (no re-encode)."""
    with _LOCK:
        if not is_recording():
            return {"ok": False, "message": "Not recording -- start capture first."}
        ff = _STATE["ffmpeg"] or find_ffmpeg()
        ring = _STATE["ring_dir"]
        seg_seconds = _STATE["settings"].get("seg_seconds", SEG_SECONDS)
    parts = _recent_segments(ring, seconds, seg_seconds)
    if not parts:
        return {"ok": False, "message": "Buffer still filling -- try again in a few seconds."}
    clips = Path(base_dir) / "clips"
    clips.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime())
    safe = "".join(c for c in (label or "clip") if c.isalnum() or c in "-_") or "clip"
    out = clips / ("%s_%s.mp4" % (stamp, safe))
    concat = "concat:" + "|".join(str(p) for p in parts)
    rc, log = _run([ff, "-hide_banner", "-loglevel", "error", "-y",
                    "-i", concat, "-c", "copy", "-movflags", "+faststart", str(out)],
                   timeout=30)
    if rc != 0 or not out.exists():
        return {"ok": False, "message": "Clip save failed: %s" % (log[:200] or "unknown")}
    return {"ok": True, "file": str(out), "name": out.name,
            "seconds": seconds, "message": "Saved %s" % out.name}


def save_full(base_dir, label="match"):
    """Concatenate the ENTIRE current recording (every ring segment) into one mp4 --
    full-match recording, not a rolling-buffer clip. Call after stop() so the final
    segment is finalized. Returns {ok, name, ...}."""
    with _LOCK:
        ff = _STATE["ffmpeg"] or find_ffmpeg()
        ring = _STATE["ring_dir"]
    if not ring or not ff:
        return {"ok": False, "message": "Not recording / no ffmpeg."}
    segs = sorted(Path(ring).glob("seg_*.ts"), key=lambda f: f.stat().st_mtime)
    if not segs:
        return {"ok": False, "message": "No footage captured."}
    clips = Path(base_dir) / "clips"
    clips.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime())
    safe = "".join(c for c in (label or "match") if c.isalnum() or c in "-_") or "match"
    out = clips / ("%s_%s.mp4" % (stamp, safe))
    concat = "concat:" + "|".join(str(p) for p in segs)
    rc, log = _run([ff, "-hide_banner", "-loglevel", "error", "-y",
                    "-i", concat, "-c", "copy", "-movflags", "+faststart", str(out)],
                   timeout=300)
    if rc != 0 or not out.exists():
        return {"ok": False, "message": "Full save failed: %s" % (log[:200] or "unknown")}
    mins = round(len(segs) * _STATE["settings"].get("seg_seconds", SEG_SECONDS) / 60.0, 1)
    return {"ok": True, "file": str(out), "name": out.name,
            "message": "Saved %s (~%s min)" % (out.name, mins)}


def extract_frame(clip_path, out_path, at_seconds=None):
    """Pull a single frame from a clip to a PNG (for the vision model to analyze).
    at_seconds=None grabs an early frame. Returns True on success."""
    ff = find_ffmpeg()
    if not ff or not Path(clip_path).exists():
        return False
    args = [ff, "-hide_banner", "-loglevel", "error", "-y"]
    if at_seconds is not None:
        args += ["-ss", str(at_seconds)]
    args += ["-i", str(clip_path), "-frames:v", "1", "-q:v", "3", str(out_path)]
    rc, _ = _run(args, timeout=25)
    return rc == 0 and Path(out_path).exists()


def _gdi_screenshot(out_path):
    """Fallback full-screen grab via GDI (no ffmpeg/ddagrab needed). Works on any
    GPU and when ddagrab returns nothing (wrong output index, no NVIDIA, etc.).
    Same path the OCR uses, so if mode/rank OCR works this does too. Note: like
    ddagrab, GDI can't grab a true FULLSCREEN-EXCLUSIVE DX game -- borderless/
    windowed is required either way."""
    # 1) PIL ImageGrab (GDI BitBlt) -- bundled, fastest, primary monitor.
    try:
        from PIL import ImageGrab
        img = ImageGrab.grab()
        img.save(str(out_path))
        if Path(out_path).exists() and Path(out_path).stat().st_size > 0:
            return True
    except Exception:
        pass
    # 2) PowerShell System.Drawing fallback (covers a missing/odd PIL).
    if os.name == "nt":
        try:
            ps = (
                "Add-Type -AssemblyName System.Drawing,System.Windows.Forms;"
                "$b=[System.Windows.Forms.Screen]::PrimaryScreen.Bounds;"
                "$bm=New-Object System.Drawing.Bitmap $b.Width,$b.Height;"
                "$g=[System.Drawing.Graphics]::FromImage($bm);"
                "$g.CopyFromScreen($b.Location,[System.Drawing.Point]::Empty,$b.Size);"
                "$bm.Save('%s');$g.Dispose();$bm.Dispose()" % str(out_path).replace("\\", "\\\\")
            )
            _run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps], timeout=15)
            return Path(out_path).exists() and Path(out_path).stat().st_size > 0
        except Exception:
            return False
    return False


def _ddagrab_shot(ff, out_path):
    """One-shot DXGI desktop-duplication grab. This is the ONLY method that can
    capture a true FULLSCREEN-EXCLUSIVE DirectX game (GDI returns black for it)."""
    args = [ff, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-filter_complex", "ddagrab=output_idx=0:framerate=2,hwdownload,format=bgra",
            "-frames:v", "1", "-update", "1", str(out_path)]
    _run(args, timeout=20)
    return Path(out_path).exists() and Path(out_path).stat().st_size > 0


def _is_blackish(path):
    """True if the grabbed frame is essentially blank/black (almost no contrast) --
    e.g. a multi-GPU ddagrab that grabbed the wrong adapter, or GDI on a fullscreen
    game. Lets us reject a dud and try the other method."""
    try:
        from PIL import Image
        im = Image.open(path).convert("L").resize((48, 27))
        lo, hi = im.getextrema()
        return (hi - lo) < 8
    except Exception:
        return False


def capture_screenshot(out_path):
    """Grab one full-screen frame to a PNG.

    CRITICAL: two DXGI desktop-duplication (ddagrab) clients CANNOT capture the same
    screen at once -- a second ddagrab CRASHES the recorder. So while recording, we
    pull a recent frame from the ROLLING BUFFER instead of opening a second ddagrab.
    Only when NOT recording do we do a fresh one-shot ddagrab. A GDI grab (used as the
    fallback throughout) is NOT a ddagrab, so it's always safe -- even mid-recording."""
    if is_recording() and _STATE.get("ring_dir"):
        try:
            segs = sorted(Path(_STATE["ring_dir"]).glob("seg_*.ts"),
                          key=lambda f: f.stat().st_mtime)
        except Exception:
            segs = []
        for seg in reversed(segs):                      # newest first
            try:
                if seg.stat().st_size > 50000 and extract_frame(str(seg), out_path, None):
                    return True
            except Exception:
                pass
        return _gdi_screenshot(out_path)   # buffer empty -> GDI (safe; not a 2nd ddagrab)
    # NOT recording: ddagrab FIRST -- it's the only method that grabs a true
    # FULLSCREEN-EXCLUSIVE game (the user's case). Validate the frame isn't black
    # (a multi-GPU output_idx mismatch yields a blank surface); if it is, fall back
    # to GDI for windowed/borderless. Keep whichever produces a real (non-black) image.
    ff = find_ffmpeg()
    if ff and _ddagrab_shot(ff, out_path) and not _is_blackish(out_path):
        return True
    if _gdi_screenshot(out_path) and not _is_blackish(out_path):
        return True
    # nothing clean: return whatever exists. If it's still black, the game is
    # fullscreen-exclusive AND ddagrab can't reach it -> Borderless/Windowed needed.
    return Path(out_path).exists() and Path(out_path).stat().st_size > 0


def extract_frames(clip_path, out_dir, offsets=(2, 14, 26)):
    """Pull several frames (at the given second offsets) from a clip, for a
    multi-frame aim/crosshair review. Returns the list of PNG paths written."""
    out = []
    for i, sec in enumerate(offsets):
        p = Path(out_dir) / ("clipframe_%d.png" % i)
        if extract_frame(clip_path, p, sec):
            out.append(str(p))
    return out


def list_clips(base_dir):
    clips = Path(base_dir) / "clips"
    items = []
    if clips.exists():
        for f in sorted(clips.glob("*.mp4"), key=lambda x: x.stat().st_mtime, reverse=True):
            try:
                st = f.stat()
                items.append({"name": f.name, "path": str(f),
                              "sizeMB": round(st.st_size / 1048576.0, 1),
                              "ts": int(st.st_mtime * 1000)})
            except Exception:
                pass
    return {"items": items, "total": len(items), "dir": str(clips)}


def status(base_dir=None):
    pr = probe()
    rec = is_recording()
    buf = _STATE["settings"].get("buffer_seconds") if rec else None
    elapsed = int(time.time() - _STATE["started"]) if rec else 0
    out = {
        "available": bool(pr["ok"]),
        "ffmpeg": pr["ffmpeg"],
        "nvenc": pr["nvenc"],
        "ddagrab": pr["ddagrab"],
        "recording": rec,
        "bufferSeconds": buf,
        "elapsed": elapsed,
        "message": pr["message"],
    }
    if base_dir is not None:
        out["clips"] = list_clips(base_dir).get("total", 0)
    return out
