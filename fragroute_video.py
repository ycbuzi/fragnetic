"""FRAGROUTE video editor -- AI-driven clip -> edit -> review over your gameplay
footage. All local, via the bundled (LGPL) ffmpeg. The recorder captures with
ddagrab (video-only), so montages add a clean silent/music track -- no audio-sync
headaches.

Capabilities:
  * trim      -- cut a sub-range out of a clip
  * montage   -- stitch several clips into one reel (normalized, optional music + title)
  * caption   -- burn a text overlay onto a clip
  * slowmo    -- speed-ramp a clip (slow-mo / speed-up)
Review/analysis lives in the engine (vision VLM + YOLO over extracted frames).

The engine sets FFMPEG (path to ffmpeg.exe) and CLIPS_DIR / OUT_DIR. Pure stdlib.
"""
import os
import subprocess
import time
from pathlib import Path

import fragroute_proc as _proc   # orphan-proof helpers (shared Windows Job Object)

APP_VIDEO_BUILD = "video-2"      # video-2: job-adopt ffmpeg transcodes so they can't orphan

FFMPEG = None              # set by engine -> ffmpeg.exe
CLIPS_DIR = None           # source clips folder
OUT_DIR = None             # where edited videos are written
_NOWIN = {"creationflags": 0x08000000 | 0x00004000} if os.name == "nt" else {}  # no window + below-normal
_STATE = {"busy": False, "last": None, "error": None}

# a Windows system font for drawtext (caption/title)
_FONT = None
for _f in (r"C:\Windows\Fonts\arialbd.ttf", r"C:\Windows\Fonts\arial.ttf", r"C:\Windows\Fonts\segoeui.ttf"):
    if os.path.exists(_f):
        _FONT = _f
        break


def available():
    return bool(FFMPEG and os.path.exists(FFMPEG))


def _out_dir():
    d = Path(OUT_DIR) if OUT_DIR else (Path(CLIPS_DIR or ".") / "edited")
    d.mkdir(parents=True, exist_ok=True)
    return d


def _ff(args, timeout=600):
    """Run ffmpeg; return (ok, tail-of-stderr). Never raises."""
    try:
        # _proc.run job-adopts the ffmpeg child so a long transcode can't orphan if
        # we're hard-killed mid-encode (a blocking run alone would leave it running).
        p = _proc.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y"] + args,
                      capture_output=True, text=True, errors="replace",
                      timeout=timeout, **_NOWIN)
        return (p.returncode == 0), (p.stderr or p.stdout or "")[-300:]
    except Exception as e:
        return False, str(e)


_ENC = {"v": None}


def _vcodec():
    """Pick the best H.264 encoder for THIS machine: NVENC (NVIDIA) / AMF (AMD) /
    QSV (Intel) hardware if available, else libopenh264 (CPU, BSD -- commercial-safe),
    else mpeg4. Delegates to the hardware probe so AMD/Intel customers also get
    hardware encoding. NOTE: the LGPL ffmpeg has NO libx264 (x264 is GPL)."""
    if _ENC["v"] is None:
        # preferred: the shared hardware-aware picker (knows AMF/QSV too)
        try:
            import fragroute_hardware as _HW
            if not _HW.FFMPEG:
                _HW.FFMPEG = FFMPEG
            args, _label, _hw = _HW.best_video_encoder()
            if args:
                _ENC["v"] = args
                return list(_ENC["v"])
        except Exception:
            pass
        # fallback: probe locally (NVENC -> libopenh264 -> mpeg4)
        enc = ""
        try:
            p = subprocess.run([FFMPEG, "-hide_banner", "-encoders"],
                               capture_output=True, text=True, errors="replace",
                               timeout=30, **_NOWIN)
            enc = p.stdout or ""
        except Exception:
            pass
        if "h264_nvenc" in enc:
            _ENC["v"] = ["-c:v", "h264_nvenc", "-preset", "p5", "-cq", "23", "-pix_fmt", "yuv420p"]
        elif "libopenh264" in enc:
            _ENC["v"] = ["-c:v", "libopenh264", "-b:v", "6M", "-pix_fmt", "yuv420p"]
        else:
            _ENC["v"] = ["-c:v", "mpeg4", "-q:v", "3", "-pix_fmt", "yuv420p"]
    return list(_ENC["v"])


def _drawtext(text, size=48, y="36"):
    t = str(text).replace("\\", "").replace(":", "\\:").replace("'", "")[:80]
    f = ("fontfile='%s':" % _FONT.replace("\\", "/").replace(":", "\\:")) if _FONT else ""
    return ("drawtext=%stext='%s':fontcolor=white:fontsize=%d:borderw=3:bordercolor=black@0.8:"
            "x=(w-text_w)/2:y=%s" % (f, t, size, y))


def frames_timed(src, fps=1.0, max_frames=240):
    """Extract frames at `fps` to a temp dir; return [(time_seconds, path)] so a
    caller can map a frame back to its timestamp in the video (for auto-highlights)."""
    if not available() or not src or not os.path.exists(src):
        return []
    import tempfile
    d = Path(tempfile.gettempdir()) / "fr_hl_frames"
    try:
        import shutil
        shutil.rmtree(d, ignore_errors=True)
    except Exception:
        pass
    d.mkdir(parents=True, exist_ok=True)
    ok, _ = _ff(["-i", src, "-vf", "fps=%g" % fps, "-frames:v", str(int(max_frames)),
                 "-q:v", "4", str(d / "f_%05d.jpg")])
    if not ok:
        return []
    out = []
    for i, p in enumerate(sorted(d.glob("f_*.jpg"))):
        out.append((i / float(fps), str(p)))   # frame i -> i/fps seconds
    return out


def trim(src, start, dur, out=None):
    """Cut `dur` seconds starting at `start` (seconds). Accurate re-encode."""
    if not available() or not src or not os.path.exists(src):
        return {"ok": False, "message": "clip/ffmpeg unavailable"}
    out = out or str(_out_dir() / ("trim_%s.mp4" % time.strftime("%Y%m%d_%H%M%S")))
    ok, err = _ff(["-ss", str(float(start)), "-i", src, "-t", str(float(dur))] +
                  _vcodec() + ["-an", out])
    return {"ok": ok, "file": out, "name": os.path.basename(out)} if ok else {"ok": False, "message": err}


def _has_audio(src):
    """True if the file has an audio stream (parse ffmpeg's probe output). The app's
    own ddagrab clips are video-only; OBS/external clips usually have game audio."""
    try:
        p = subprocess.run([FFMPEG, "-hide_banner", "-i", src],
                           capture_output=True, text=True, errors="replace",
                           timeout=30, **_NOWIN)
        return "Audio:" in (p.stderr or "")
    except Exception:
        return False


def _normalize(src, out, w=1920, h=1080, fps=30):
    """Scale+pad a clip to WxH @ fps. PRESERVES the clip's own audio if it has any
    (so game audio survives a montage); adds a silent stereo track only when the
    clip is video-only. Every normalized clip ends with a stereo aac track so the
    concat demuxer can stitch a mixed set."""
    vf = ("scale=%d:%d:force_original_aspect_ratio=decrease,pad=%d:%d:(ow-iw)/2:(oh-ih)/2,"
          "setsar=1,fps=%d" % (w, h, w, h, fps))
    common = ["-vf", vf] + _vcodec() + ["-c:a", "aac", "-b:a", "160k", "-ar", "44100", "-ac", "2"]
    if _has_audio(src):
        return _ff(["-i", src, "-map", "0:v:0", "-map", "0:a:0"] + common + [out])
    return _ff(["-i", src, "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
                "-map", "0:v:0", "-map", "1:a:0", "-shortest"] + common + [out])


def montage(clips, out=None, music=None, title=None):
    """Stitch clips into one reel: normalize each -> concat -> optional music + title.
    `clips` = list of file paths (in order). Returns {ok, file}."""
    if not available():
        return {"ok": False, "message": "ffmpeg unavailable"}
    clips = [c for c in (clips or []) if c and os.path.exists(c)]
    if not clips:
        return {"ok": False, "message": "no clips to montage"}
    with _LOCK:
        if _STATE["busy"]:
            return {"ok": False, "message": "already editing"}
        _STATE["busy"] = True
    try:
        import tempfile
        tmp = Path(tempfile.gettempdir())
        normd = []
        for i, c in enumerate(clips):
            n = str(tmp / ("fr_norm_%d.mp4" % i))
            ok, err = _normalize(c, n)
            if ok:
                normd.append(n)
        if not normd:
            return {"ok": False, "message": "couldn't normalize clips"}
        # concat via demuxer (all clips now share format)
        listf = str(tmp / "fr_concat.txt")
        with open(listf, "w", encoding="utf-8") as fh:
            for n in normd:
                fh.write("file '%s'\n" % n.replace("\\", "/"))
        concat = str(tmp / "fr_concat.mp4")
        ok, err = _ff(["-f", "concat", "-safe", "0", "-i", listf, "-c", "copy", concat])
        if not ok:
            return {"ok": False, "message": "concat failed: %s" % err}
        out = out or str(_out_dir() / ("montage_%s.mp4" % time.strftime("%Y%m%d_%H%M%S")))
        # optional: title overlay (first ~3s) and/or background music MIXED UNDER the
        # clips' own audio (game audio survives; music sits lower at 0.35).
        title_vf = ("%s:enable='lt(t,3)'" % _drawtext(title, size=64, y="60")) if title else None
        if music and os.path.exists(music):
            fc = "[0:a][1:a]amix=inputs=2:duration=first:dropout_transition=0:weights=1 0.35[a]"
            maps = ["-map", "[a]"]
            if title_vf:
                fc = "[0:v]%s[v];%s" % (title_vf, fc)
                maps = ["-map", "[v]"] + maps
            else:
                maps = ["-map", "0:v:0"] + maps
            ok, err = _ff(["-i", concat, "-i", music, "-filter_complex", fc] + maps +
                          ["-shortest"] + _vcodec() + ["-c:a", "aac", "-b:a", "192k", out])
        elif title_vf:
            ok, err = _ff(["-i", concat, "-vf", title_vf] + _vcodec() + ["-c:a", "copy", out])
        else:
            import shutil
            shutil.copy2(concat, out); ok = True
        # cleanup temp
        for n in normd + [listf, concat]:
            try:
                os.remove(n)
            except Exception:
                pass
        if ok:
            _STATE["last"] = out
            return {"ok": True, "file": out, "name": os.path.basename(out), "clips": len(normd)}
        return {"ok": False, "message": err}
    finally:
        _STATE["busy"] = False


def caption(src, text, out=None):
    """Burn a centered text caption onto a clip."""
    if not available() or not src or not os.path.exists(src):
        return {"ok": False, "message": "clip/ffmpeg unavailable"}
    out = out or str(_out_dir() / ("caption_%s.mp4" % time.strftime("%Y%m%d_%H%M%S")))
    ok, err = _ff(["-i", src, "-vf", _drawtext(text, size=52, y="h-text_h-40")] +
                  _vcodec() + ["-c:a", "copy", out])
    return {"ok": ok, "file": out, "name": os.path.basename(out)} if ok else {"ok": False, "message": err}


def slowmo(src, factor=0.5, out=None):
    """Speed-ramp a clip. factor<1 = slow-mo, >1 = speed-up (video only)."""
    if not available() or not src or not os.path.exists(src):
        return {"ok": False, "message": "clip/ffmpeg unavailable"}
    factor = max(0.25, min(4.0, float(factor)))
    out = out or str(_out_dir() / ("speed_%s.mp4" % time.strftime("%Y%m%d_%H%M%S")))
    ok, err = _ff(["-i", src, "-vf", "setpts=%.3f*PTS" % (1.0 / factor), "-an"] +
                  _vcodec() + [out])
    return {"ok": ok, "file": out, "name": os.path.basename(out)} if ok else {"ok": False, "message": err}


def list_edits():
    d = _out_dir()
    out = []
    for f in sorted(d.glob("*.mp4"), key=lambda x: x.stat().st_mtime, reverse=True):
        try:
            st = f.stat()
            out.append({"name": f.name, "path": str(f), "sizeMB": round(st.st_size / 1048576.0, 2),
                        "ts": int(st.st_mtime * 1000)})
        except Exception:
            pass
    return {"items": out, "total": len(out)}


def status():
    return {"build": APP_VIDEO_BUILD, "available": available(),
            "busy": _STATE["busy"], "font": bool(_FONT), "error": _STATE.get("error")}


import threading
_LOCK = threading.Lock()
