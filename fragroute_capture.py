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

import fragroute_proc as _proc   # orphan-proof helpers (shared Windows Job Object)

# WASAPI loopback audio (records the REAL default output, not silent Stereo Mix).
# Optional -- if unavailable we fall back to the old dshow-loopback path.
try:
    import fragroute_audio
except Exception:
    fragroute_audio = None

# Windows Graphics Capture -- records ONLY the FragPunk window, so windows overlaying
# the game don't land in the clip. Optional; falls back to ddagrab (whole monitor).
try:
    import fragroute_wgc
except Exception:
    fragroute_wgc = None

APP_CAPTURE_BUILD = "cap-4"   # cap-4: WGC game-only video capture (overlays excluded), ddagrab fallback

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
        p = subprocess.run(args, capture_output=True, text=True, errors="replace",
                           timeout=timeout, creationflags=flags)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except Exception as e:
        return -1, str(e)


def probe(refresh=False):
    """Check what this ffmpeg can do: an H.264 encoder + the ddagrab capture filter.
    Encoder preference: NVENC (NVIDIA, ~0 FPS cost) > AMD h264_amf / Intel h264_qsv
    (also hardware, low cost) > libx264 SOFTWARE (works on ANY GPU incl. AMD/Intel/
    integrated -- the universal floor; costs some CPU). Recording is available if
    ddagrab is present AND at least one of those encoders exists."""
    if _STATE["probe"] is not None and not refresh:
        return _STATE["probe"]
    ff = find_ffmpeg(refresh=refresh)
    out = {"ffmpeg": ff, "ok": False, "nvenc": [], "hw": [], "software": False,
           "ddagrab": False, "message": ""}
    if not ff:
        out["message"] = ("ffmpeg not found. Drop ffmpeg.exe next to Fragnetic.exe "
                          "(a full build with ddagrab) to enable recording.")
        _STATE["probe"] = out
        return out
    rc, enc = _run([ff, "-hide_banner", "-encoders"])
    for name in ("h264_nvenc", "hevc_nvenc", "av1_nvenc"):
        if name in enc:
            out["nvenc"].append(name)
    for name in ("h264_amf", "h264_qsv"):          # AMD / Intel hardware encoders
        if name in enc:
            out["hw"].append(name)
    out["software"] = ("libx264" in enc)
    rc2, filt = _run([ff, "-hide_banner", "-filters"])
    out["ddagrab"] = ("ddagrab" in filt)
    has_enc = bool(out["nvenc"] or out["hw"] or out["software"])
    if out["ddagrab"] and has_enc:
        out["ok"] = True
        if out["nvenc"]:
            out["message"] = "Ready: NVENC (0-cost GPU encode) + desktop-duplication capture."
        elif out["hw"]:
            out["message"] = "Ready: %s hardware encoder + desktop capture." % out["hw"][0]
        else:
            out["message"] = ("Ready: software (libx264) recording -- works on any GPU, "
                              "uses some CPU while recording.")
    elif not out["ddagrab"]:
        out["message"] = "This ffmpeg lacks the ddagrab filter; need ffmpeg 6.0+ (full build)."
    else:
        out["message"] = "This ffmpeg has no usable H.264 encoder (nvenc / amf / qsv / libx264)."
    _STATE["probe"] = out
    return out


def _pick_encoder(pr, requested=None):
    """Choose the best available encoder from a probe result: NVENC > AMD/Intel HW >
    software libx264. A valid user `requested` override wins."""
    avail = list(pr.get("nvenc") or []) + list(pr.get("hw") or [])
    if pr.get("software"):
        avail.append("libx264")
    if requested and requested in avail:
        return requested
    if pr.get("nvenc"):
        return pr["nvenc"][0]
    if pr.get("hw"):
        return pr["hw"][0]
    return "libx264"


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
                               capture_output=True, text=True, errors="replace",
                               timeout=15,
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
    is_nvenc = encoder.endswith("_nvenc")
    # ddagrab yields D3D11 GPU frames. NVENC consumes those directly (zero-copy GPU
    # encode). Software (libx264) and the AMD/Intel encoders need them in SYSTEM memory,
    # so hwdownload them to nv12 first (a little CPU, but recording then works on ANY GPU).
    grab = "ddagrab=output_idx=0:framerate=%d" % int(fps)
    if not is_nvenc:
        # ddagrab's desktop surface is BGRA. hwdownload can only output a format the
        # GPU frame actually holds -- downloading straight to nv12 errors ("Invalid
        # output format nv12 for hwframe download"). So download AS bgra, THEN convert
        # to nv12 in system memory for the AMD/Intel/software encoder. (Verified live
        # on an AMD Radeon iGPU: h264_amf produces valid segments with this chain.)
        grab += ",hwdownload,format=bgra,format=nv12"
    if audio_device:
        grab += "[v]"
    cmd += ["-filter_complex", grab]
    if audio_device:
        cmd += ["-map", "[v]", "-map", "0:a"]
    cmd += ["-c:v", encoder]
    # encoder-appropriate speed preset (all chosen to stay light so in-game FPS holds):
    if is_nvenc:
        cmd += ["-preset", "p5", "-tune", "hq"]          # NVENC balanced, light on the encoder block
    elif encoder == "h264_amf":
        cmd += ["-usage", "transcoding", "-quality", "speed"]   # AMD hardware
    elif encoder == "h264_qsv":
        cmd += ["-preset", "veryfast"]                   # Intel QuickSync hardware
    else:                                                # libx264 SOFTWARE fallback
        cmd += ["-preset", "ultrafast", "-pix_fmt", "yuv420p"]  # 'ultrafast' = lowest CPU / least game impact
    cmd += ["-b:v", str(bitrate),
            # Force standard SDR BT.709 limited-range color tags. ddagrab's desktop
            # surface carries sRGB / odd-primary (bt470bg) metadata that the encoder
            # propagates, so players apply the wrong gamma/primaries and the clip looks
            # washed-out / over-bright (the 'shine/glare'). Metadata only -- no filter,
            # so ZERO FPS cost (keeps the capture game-friendly). NOTE: if Windows HDR is
            # ON, that's a separate cause -- toggle HDR off for recording, or we add an
            # opt-in tonemap (which does cost CPU).
            "-color_range", "tv", "-colorspace", "bt709",
            "-color_primaries", "bt709", "-color_trc", "bt709"]
    if gpu is not None and is_nvenc:
        cmd += ["-gpu", str(int(gpu))]                   # GPU pin only applies to NVENC          # only if explicitly pinned (default None)
    if audio_device:
        cmd += ["-c:a", "aac", "-b:a", "160k"]
    cmd += ["-f", "segment", "-segment_time", str(int(seg_seconds))]
    # ring_segments > 0 => rolling highlight buffer (wrap/overwrite oldest).
    # ring_segments <= 0 => FULL-MATCH: OMIT -segment_wrap entirely so ffmpeg keeps
    # every segment (unlimited). NOTE: '-segment_wrap 0' does NOT mean unlimited --
    # ffmpeg wraps at index 0 and the recording never accumulates, so we must leave
    # the option off. Use a 4-digit index so a long match never runs out of names.
    if int(ring_segments) > 0:
        cmd += ["-segment_wrap", str(int(ring_segments))]
    cmd += ["-segment_format", "mpegts", "-reset_timestamps", "1",
            str(Path(ring_dir) / ("seg_%03d.ts" if int(ring_segments) > 0 else "seg_%04d.ts"))]
    return cmd


def _build_wgc_cmd(ff, ring_dir, w, h, fps, bitrate, encoder, seg_seconds, ring_segments):
    """ffmpeg command for the WGC path: raw BGRA frames on stdin (fed by the pump
    thread from the game window) -> NVENC -> the SAME mpegts segment ring the ddagrab
    path uses, so save/concat/mux downstream is identical. Video-only (audio is the
    separate WASAPI WAV, muxed on save)."""
    is_nvenc = encoder.endswith("_nvenc")
    cmd = [ff, "-hide_banner", "-loglevel", "warning", "-y",
           "-f", "rawvideo", "-pixel_format", "bgra",
           "-video_size", "%dx%d" % (int(w), int(h)), "-framerate", str(int(fps)),
           # stamp frames by real arrival time (Python can't feed an EXACT fps), so the
           # video's duration equals wall-clock and stays in sync with the audio WAV;
           # -r below re-samples to steady CFR out.
           "-use_wallclock_as_timestamps", "1",
           "-i", "pipe:0", "-c:v", encoder, "-r", str(int(fps))]
    if is_nvenc:
        cmd += ["-preset", "p5", "-tune", "hq"]
    elif encoder == "h264_amf":
        cmd += ["-usage", "transcoding", "-quality", "speed"]
    elif encoder == "h264_qsv":
        cmd += ["-preset", "veryfast"]
    else:
        cmd += ["-preset", "ultrafast"]
    cmd += ["-pix_fmt", "yuv420p", "-b:v", str(bitrate),
            # a keyframe every segment so the muxer can cut ON TIME (otherwise mpegts
            # only splits at the next IDR and segments run long -> coarse rolling buffer).
            "-g", str(max(1, int(fps) * int(seg_seconds))),
            "-color_range", "tv", "-colorspace", "bt709",
            "-color_primaries", "bt709", "-color_trc", "bt709",
            "-f", "segment", "-segment_time", str(int(seg_seconds))]
    if int(ring_segments) > 0:
        cmd += ["-segment_wrap", str(int(ring_segments))]
    cmd += ["-segment_format", "mpegts", "-reset_timestamps", "1",
            str(Path(ring_dir) / ("seg_%03d.ts" if int(ring_segments) > 0 else "seg_%04d.ts"))]
    return cmd


def _start_wgc(ff, ring, hwnd, fps, bitrate, encoder, seg, nseg, flags):
    """Open a WGC session on the game window, spawn the stdin-fed ffmpeg, and start a
    pump thread that writes frames at a steady fps. Returns (proc, state_dict) on
    success or (None, None) so the caller falls back to ddagrab. The pump reuses the
    last frame when the window hasn't produced a new one, so timing stays even; per
    frame it's a ~1ms GPU->CPU copy (measured), negligible for in-game FPS."""
    if fragroute_wgc is None:
        return None, None
    sess = fragroute_wgc._open_session(hwnd)
    if not sess:
        return None, None
    w, h = sess["w"], sess["h"]
    # Prove WGC actually DELIVERS frames for this window before committing to it. Some
    # cases (exclusive-fullscreen quirks, an occluded/minimized window, a driver issue)
    # open a valid session but never produce a frame -- without this check the pump would
    # write black forever and we'd never fall back. No frame in ~1.5s => bail to ddagrab.
    first = fragroute_wgc._grab(sess, timeout_s=1.5)
    if first is None:
        fragroute_wgc._close(sess)
        return None, None
    video_started = time.time()   # real video content begins ~now (first frame in hand);
                                  # used for accurate A/V head-alignment on save
    try:
        cmd = _build_wgc_cmd(ff, ring, w, h, fps, bitrate, encoder, seg, nseg)
        p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, creationflags=flags)
    except Exception:
        fragroute_wgc._close(sess)
        return None, None
    _proc.adopt(p)   # OS kills the encoder if we die by any means
    stop_ev = threading.Event()

    def _pump():
        interval = 1.0 / float(fps)
        next_t = time.time()
        last = first        # seed with the proven first frame -> no black at clip start
        blank = b"\x00" * (w * h * 4)
        while not stop_ev.is_set():
            data = fragroute_wgc._grab(sess, timeout_s=interval)
            if data is not None:
                last = data
            try:
                p.stdin.write(last if last is not None else blank)
            except (BrokenPipeError, OSError, ValueError):
                break
            next_t += interval
            slp = next_t - time.time()
            if slp > 0:
                time.sleep(slp)
            else:
                next_t = time.time()   # fell behind -> don't spiral
        try:
            p.stdin.close()            # EOF -> ffmpeg finalizes the last segment
        except Exception:
            pass

    th = threading.Thread(target=_pump, daemon=True)
    th.start()
    # make sure ffmpeg didn't reject the input/encoder immediately
    time.sleep(0.5)
    if p.poll() is not None:
        stop_ev.set()
        fragroute_wgc._close(sess)
        return None, None
    return p, {"session": sess, "thread": th, "stop": stop_ev, "w": w, "h": h,
               "video_started": video_started}


def is_recording():
    p = _STATE["proc"]
    return p is not None and (p.poll() is None)


def elapsed():
    """Seconds the current recording has been running (0 when idle). Lets callers enforce a
    max-duration cap so a stuck match-state machine can't record across many matches into one
    unbounded file."""
    try:
        if not is_recording():
            return 0
        st = _STATE.get("started") or 0
        return max(0, int(time.time() - st)) if st else 0
    except Exception:
        return 0


def has_footage(base_dir=None):
    """True if the current ring has real segments worth saving -- lets us salvage a
    full-match recording even if the ffmpeg process died mid-match."""
    ring = _STATE.get("ring_dir")
    if not ring:
        return False
    try:
        return any(p.stat().st_size > 50000 for p in Path(ring).glob("seg_*.ts"))
    except Exception:
        return False


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
        encoder = _pick_encoder(pr, opts.get("encoder"))   # NVENC > AMD/Intel HW > libx264
        gpu = opts.get("gpu", DEFAULT_GPU)
        seg = int(opts.get("seg_seconds", SEG_SECONDS))
        nseg = int(opts.get("ring_segments", RING_SEGMENTS))
        # AUDIO: capture the game sound. PREFERRED path = WASAPI loopback of the real
        # default-output device (fragroute_audio) -- works for USB/HDMI/Bluetooth/Yeti
        # outputs where "Stereo Mix (Realtek)" is dead silent. We record that to a
        # separate WAV and mux it in on save, so ffmpeg stays VIDEO-ONLY (no dshow).
        # Only if WASAPI loopback is unavailable do we fall back to the old dshow
        # Stereo-Mix path (which is muxed straight into the segments).
        want_audio = bool(opts.get("record_audio", True))
        wasapi_audio = False
        if want_audio and fragroute_audio is not None and fragroute_audio.available():
            try:
                wasapi_audio = fragroute_audio.start(str(ring))
            except Exception:
                wasapi_audio = False
        audio_device = None
        if want_audio and not wasapi_audio:                 # fallback: dshow Stereo Mix
            audio_device = opts.get("audio_device") or detect_audio_loopback()
        flags = 0x08000000 if os.name == "nt" else 0

        def _launch(adev):
            cmd = _build_capture_cmd(ff, ring, fps, bitrate, encoder, gpu, seg, nseg, audio_device=adev)
            p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, creationflags=flags)
            _proc.adopt(p)   # OS kills the recorder if we die by any means (no orphan mid-recording)
            return p

        # VIDEO SOURCE -- prefer WGC game-window capture so overlays (browser/Discord
        # over the game) never land in the clip. Needs the FragPunk window; falls back
        # to ddagrab (whole monitor) if WGC is unavailable, the window isn't found, or
        # ffmpeg rejects the stdin feed. Audio is unchanged (separate WASAPI WAV).
        _STATE["wgc"] = None
        proc = None
        video_mode = "screen"
        if bool(opts.get("gameOnly", True)) and fragroute_wgc is not None and fragroute_wgc.available():
            try:
                hwnd = fragroute_wgc.find_fragpunk_hwnd(opts.get("game_pids"))
            except Exception:
                hwnd = None
            if hwnd:
                try:
                    proc, wgc_state = _start_wgc(ff, ring, hwnd, fps, bitrate, encoder, seg, nseg, flags)
                except Exception:
                    proc, wgc_state = None, None
                if proc is not None:
                    _STATE["wgc"] = wgc_state
                    video_mode = "game"
        if proc is not None:
            # WGC path is live; skip the ddagrab launch + its audio/encoder retries.
            audio_device = None if wasapi_audio else audio_device
        else:
            # --- ddagrab fallback: whole-monitor capture (WGC unavailable/no window) ---
            # stdin=DEVNULL (+ -nostdin in the cmd): a PIPE stdin makes ffmpeg quit
            # on a spurious EOF/keypress -- that was killing the recorder after ~6s.
            try:
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
            # ENCODER fallback: some AMD/Intel drivers reject their HW encoder. If the
            # chosen one died at startup, retry with software libx264 so recording still
            # works on ANY machine. (nvenc is reliable, so we skip this on the common path.)
            if encoder in ("h264_amf", "h264_qsv") and pr.get("software"):
                time.sleep(1.0)
                if proc.poll() is not None:
                    encoder = "libx264"
                    try:
                        proc = _launch(audio_device)
                    except Exception as e:
                        return {"ok": False, "message": "Failed to start ffmpeg: %s" % e}
        # if the WASAPI loopback thread died immediately, don't claim we have audio
        if wasapi_audio and fragroute_audio is not None and not fragroute_audio.is_recording():
            wasapi_audio = False
        _STATE["audio_device"] = audio_device
        _STATE["wasapi_audio"] = wasapi_audio
        _STATE["proc"] = proc
        _STATE["ring_dir"] = ring
        # For WGC, anchor "started" to the TRUE first-frame time (the video's wall-clock
        # t=0), not now -- start() spent ~0.5s on the first-frame proof + poll, and the
        # save-time A/V head-alignment (_save_concat) keys off this, so an over-late
        # value would make the audio lead the video by ~0.5s.
        _wgc_st = _STATE.get("wgc") or {}
        _STATE["started"] = _wgc_st.get("video_started") or time.time()
        _STATE["settings"] = {"fps": fps, "bitrate": bitrate, "encoder": encoder,
                              "gpu": gpu, "seg_seconds": seg, "ring_segments": nseg,
                              "buffer_seconds": seg * nseg,
                              "video": video_mode,   # "game" (WGC) or "screen" (ddagrab)
                              "audio": ("loopback" if wasapi_audio else audio_device)}
        vmsg = "game window" if video_mode == "game" else "full screen"
        if wasapi_audio:
            # reflect the REAL capture mode: 20.2+ records only FragPunk's audio
            # (per-process WASAPI loopback); older Windows falls back to the whole
            # default-output mix. Reading the wrong one would misreport the fix.
            _amode = ""
            try:
                _amode = (fragroute_audio.status() or {}).get("mode") or ""
            except Exception:
                _amode = ""
            amsg = " + audio (%s)" % {"process": "game only",
                                      "system": "system loopback"}.get(_amode, "game")
        elif audio_device:
            amsg = " + audio (%s)" % audio_device
        else:
            amsg = " (video only)"
        # nseg == 0 => ffmpeg segment_wrap 0 => UNLIMITED segments = full-match record
        # (no rolling overwrite); otherwise it's the rolling N-second highlight buffer.
        mode = "full match" if nseg == 0 else ("rolling %ds buffer" % (seg * nseg))
        return {"ok": True, "message": "Recording %s (%s)%s." % (vmsg, mode, amsg),
                "settings": _STATE["settings"]}


def stop():
    # stop the loopback audio thread FIRST so the WAV is finalized before any save
    if _STATE.get("wasapi_audio") and fragroute_audio is not None:
        try:
            fragroute_audio.stop()
        except Exception:
            pass
    # WGC path: stop the frame pump + close ffmpeg's stdin so it finalizes the last
    # segment on EOF (a raw terminate would truncate it), then tear down the session.
    wgc = _STATE.get("wgc")
    if wgc:
        try:
            wgc["stop"].set()
        except Exception:
            pass
        p = _STATE.get("proc")
        try:
            th = wgc.get("thread")
            if th:
                th.join(timeout=3)          # pump closes stdin on exit -> ffmpeg EOF
        except Exception:
            pass
        try:
            if p is not None:
                p.wait(timeout=5)           # let ffmpeg finalize on EOF before terminate
        except Exception:
            pass
        try:
            if wgc.get("session") and fragroute_wgc is not None:
                fragroute_wgc._close(wgc["session"])
        except Exception:
            pass
        _STATE["wgc"] = None
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


def _save_concat(ff, parts, out, timeout, tail_seconds=None):
    """Concatenate `parts` (video-only .ts segments) into `out`, muxing in the
    WASAPI-loopback WAV when we recorded one (so clips actually have game sound).
    tail_seconds trims the audio to the last N sec for a rolling clip. Falls back to
    a plain stream-copy when there's no separate audio (dshow path / video-only).
    Returns (rc, log)."""
    # The ring pruner runs concurrently and deletes the OLDEST segments to keep the buffer
    # bounded, so a path selected a moment ago in _recent_segments() may already be gone (or a
    # just-rotated one still be 0 bytes). Drop those NOW -- otherwise the concat demuxer (or the
    # concat: fallback) fails the ENTIRE save on one missing file and the clip/match is lost.
    _existing = []
    for _p in parts:
        try:
            _rp = Path(_p).resolve()
            if _rp.exists() and _rp.stat().st_size > 0:
                _existing.append(_rp)
        except Exception:
            pass
    if not _existing:
        return 1, "no readable segments (ring rotated) -- try again in a moment"
    parts = _existing
    # ffmpeg concat DEMUXER via a list file -- a long full match is hundreds of .ts
    # segments, and the "concat:a|b|...|z" protocol string blows past Windows'
    # command-line length limit (WinError 206 "filename too long"), failing the WHOLE
    # save and losing the match. A list file has no length cap. -safe 0 permits absolute
    # paths (the app dir has spaces).
    listfile = None
    try:
        _lf = Path(out).with_suffix(".concat.txt")
        with open(_lf, "w", encoding="utf-8") as _fh:
            for _p in parts:
                # forward slashes: universally accepted by ffmpeg on Windows and avoids
                # any concat-demuxer backslash-escape ambiguity. Single-quote-escape too.
                _ap = str(Path(_p).resolve()).replace("\\", "/").replace("'", "'\\''")
                _fh.write("file '%s'\n" % _ap)
        listfile = _lf
    except Exception:
        listfile = None
    concat_in = (["-f", "concat", "-safe", "0", "-i", str(listfile)] if listfile
                 else ["-i", "concat:" + "|".join(str(p) for p in parts)])

    def _cleanup_listfile():
        try:
            if listfile:
                os.remove(listfile)
        except Exception:
            pass
    wav = None
    if _STATE.get("wasapi_audio") and fragroute_audio is not None:
        try:
            tmp = Path(out).with_suffix(".mux.wav")
            # Full-match save: the audio thread started BEFORE ffmpeg's video (device
            # open + ~0.4s warmup), so the WAV leads the video. Drop that lead off the
            # audio head to keep A/V in sync. Rolling clips take the tail of both,
            # anchored to 'now', so they don't need this.
            head = 0.0
            if tail_seconds is None:
                try:
                    a0 = fragroute_audio.started_at()
                    v0 = float(_STATE.get("started", 0) or 0)
                    if a0 and v0 and v0 > a0:
                        head = min(5.0, v0 - a0)
                except Exception:
                    head = 0.0
            wav = fragroute_audio.snapshot(str(tmp), tail_seconds=tail_seconds, head_seconds=head)
        except Exception:
            wav = None
    if wav and Path(wav).exists() and Path(wav).stat().st_size > 1024:
        rc, log = _run([ff, "-hide_banner", "-loglevel", "error", "-y",
                        *concat_in, "-i", wav,
                        "-map", "0:v:0", "-map", "1:a:0",
                        "-c:v", "copy", "-c:a", "aac", "-b:a", "160k",
                        "-movflags", "+faststart", "-shortest", str(out)],
                       timeout=timeout)
        try:
            os.remove(wav)
        except Exception:
            pass
        if rc == 0 and Path(out).exists():
            _cleanup_listfile()
            return rc, log
        # mux failed -> fall through to a video-only copy so we never lose footage
    rc, log = _run([ff, "-hide_banner", "-loglevel", "error", "-y",
                    *concat_in, "-c", "copy", "-movflags", "+faststart", str(out)],
                   timeout=timeout)
    _cleanup_listfile()
    return rc, log


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
    # Match the audio window to the ACTUAL video length (rounded-up segments), not the
    # requested seconds -- otherwise the audio tail is shorter than the video and the
    # two drift out of sync. Both then cover the same last-N-seconds window.
    rc, log = _save_concat(ff, parts, out, timeout=45, tail_seconds=len(parts) * seg_seconds)
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
    rc, log = _save_concat(ff, segs, out, timeout=300, tail_seconds=None)
    if rc != 0 or not out.exists():
        return {"ok": False, "message": "Full save failed: %s" % (log[:200] or "unknown")}
    mins = round(len(segs) * _STATE["settings"].get("seg_seconds", SEG_SECONDS) / 60.0, 1)
    return {"ok": True, "file": str(out), "name": out.name,
            "message": "Saved %s (~%s min)" % (out.name, mins)}


def _free_disk_gb(path):
    try:
        import shutil
        return shutil.disk_usage(str(path)).free / (1024.0 ** 3)
    except Exception:
        return None


def recordings_usage(base_dir):
    """Current size of the recordings folder + free disk, for the UI."""
    clips = Path(base_dir) / "clips"
    used = 0
    n = 0
    if clips.exists():
        for f in clips.glob("*.mp4"):
            try:
                used += f.stat().st_size
                n += 1
            except Exception:
                pass
    return {"usedGB": round(used / (1024.0 ** 3), 2), "count": n,
            "freeGB": round(_free_disk_gb(clips) or 0, 1)}


def prune_recordings(base_dir, max_gb=40, min_free_gb=5):
    """Disk-sensitive auto-cleanup: delete OLDEST recordings until the folder is
    under `max_gb` AND the disk has at least `min_free_gb` free. Never touches a
    file that's currently being written (the ring/ segments are separate). Returns
    {deleted, freedMB, usedGB}."""
    clips = Path(base_dir) / "clips"
    if not clips.exists():
        return {"deleted": 0, "freedMB": 0, "usedGB": 0}
    files = sorted(clips.glob("*.mp4"), key=lambda f: f.stat().st_mtime)  # oldest first
    try:
        used = sum(f.stat().st_size for f in files)
    except Exception:
        used = 0
    max_bytes = int(float(max_gb) * (1024.0 ** 3))
    free = _free_disk_gb(clips)
    deleted, freed = 0, 0
    for f in files:
        over_cap = used > max_bytes
        low_disk = (free is not None and free < float(min_free_gb))
        if not over_cap and not low_disk:
            break
        try:
            sz = f.stat().st_size
            f.unlink()
            used -= sz
            freed += sz
            deleted += 1
            if free is not None:
                free += sz / (1024.0 ** 3)
        except Exception:
            pass
    return {"deleted": deleted, "freedMB": round(freed / 1048576), "usedGB": round(used / (1024.0 ** 3), 2)}


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
