"""WASAPI loopback audio capture -- records the DEFAULT OUTPUT device (whatever it
actually is: USB headset, HDMI monitor, a Yeti's speaker jack, Realtek...) so
recordings have REAL game sound.

Why this exists: the old path captured "Stereo Mix (Realtek)" via ffmpeg dshow.
Stereo Mix only mirrors the Realtek chip, so if the user's sound goes anywhere
else (USB/HDMI/Bluetooth) the recording is digital silence (-90 dB) even though a
(silent) AAC track is present. WASAPI loopback records the *default render
endpoint* directly, so it always matches what the user actually hears.

Pure-python via pyaudiowpatch (a PyAudio fork with WASAPI loopback -- MIT). Runs a
background thread that streams the loopback to a growing 16-bit PCM WAV in the ring
dir; the recorder muxes that WAV (or its tail) into the saved clip. Degrades
gracefully: if the lib/device is missing it just reports unavailable and the
recorder keeps working (video-only).
"""
import math
import os
import threading
import time
import wave
from pathlib import Path

APP_AUDIO_BUILD = "audio-2"   # audio-2: skip virtual/streaming sinks (Steam/NVIDIA)
                              # that yield silent loopbacks; prefer the real output +
                              # optional manual output picker (fixes no-sound clips)

try:
    import pyaudiowpatch as _pa
    _HAVE = True
except Exception:
    _pa = None
    _HAVE = False

_LOCK = threading.Lock()
_STATE = {
    "thread": None, "stop": False, "wav": None, "started": 0.0,
    "err": "", "level": 0.0, "device": "", "sr": 0, "ch": 0, "frames": 0,
}


def available():
    return _HAVE


# Virtual / streaming render endpoints that are frequently the Windows "default
# output" but carry NO real game audio -- a WASAPI loopback of one of these yields
# ZERO frames, so clips come out silent (or with no audio track at all). We skip
# them whenever a real hardware endpoint exists. (Root cause of the "recorded
# videos have no sound" bug: the default output resolved to "Steam Streaming
# Microphone", a dead virtual sink.)
_VIRTUAL_MARKERS = (
    "steam streaming", "nvidia broadcast", "vb-audio", "vb audio", "voicemeeter",
    "cable output", "cable input", "cable-a", "cable-b", "virtual", "vaio",
    "rtx voice", "synchronous audio", "remote audio", "wave link stream",
)

# Optional user override: a substring of the OUTPUT device name to record. When
# set (via set_preferred_output), it wins over auto-selection -- the reliable
# escape hatch, mirroring the mic selector.
PREFERRED_OUTPUT = None


def set_preferred_output(name):
    """Pin the output device to record (substring match), or clear with '' / None."""
    global PREFERRED_OUTPUT
    PREFERRED_OUTPUT = (name or "").strip() or None
    return PREFERRED_OUTPUT


def _is_virtual(name):
    n = (name or "").lower()
    return any(m in n for m in _VIRTUAL_MARKERS)


def _loopback_devices(p):
    """Every WASAPI loopback device on the system (real + virtual)."""
    out = []
    try:
        for d in p.get_loopback_device_info_generator():
            out.append(d)
    except Exception:
        pass
    if not out:                       # manual fallback scan
        try:
            for i in range(p.get_device_count()):
                try:
                    d = p.get_device_info_by_index(i)
                except Exception:
                    continue
                if d.get("isLoopbackDevice"):
                    out.append(d)
        except Exception:
            pass
    return out


def _default_render_name(p):
    try:
        wasapi = p.get_host_api_info_by_type(_pa.paWASAPI)
        return p.get_device_info_by_index(wasapi["defaultOutputDevice"]).get("name", "")
    except Exception:
        return ""


def _find_loopback(p):
    """Pick the loopback device to record, in priority order:
      1) an explicit user override (PREFERRED_OUTPUT substring),
      2) the REAL default render endpoint's loopback -- unless it's a virtual sink,
      3) a non-virtual loopback whose name matches the default output,
      4) the first NON-virtual loopback (skips Steam/NVIDIA virtual devices),
      5) last resort: the default (even if virtual) / the first device found.
    This ordering is what keeps clips from coming out silent when a virtual
    streaming device is the Windows default output."""
    cands = _loopback_devices(p)
    if not cands:
        return None
    # 1) explicit user override
    if PREFERRED_OUTPUT:
        pref = PREFERRED_OUTPUT.lower()
        for d in cands:
            if pref in d.get("name", "").lower():
                return d
    # 2) the actual default render endpoint's loopback (pyaudiowpatch helper),
    #    but only if it's a real device -- never a dead virtual sink.
    dfl = None
    try:
        dfl = p.get_default_wasapi_loopback()
    except Exception:
        dfl = None
    if dfl and not _is_virtual(dfl.get("name", "")):
        return dfl
    # 3) match the host-api default output name among non-virtual candidates
    name = _default_render_name(p)
    if name:
        base = name.split(" (")[0]
        for d in cands:
            if not _is_virtual(d.get("name", "")) and base and base in d.get("name", ""):
                return d
    # 4) first non-virtual loopback
    for d in cands:
        if not _is_virtual(d.get("name", "")):
            return d
    # 5) nothing clean -- take the default (even virtual) or the first device
    return dfl or cands[0]


def list_outputs():
    """All loopback (output) device names, for a UI picker. Marks the auto-pick and
    flags virtual devices so the user can avoid the silent ones."""
    if not _HAVE:
        return {"have": False, "items": [], "auto": None}
    p = _pa.PyAudio()
    try:
        cands = _loopback_devices(p)
        auto = _find_loopback(p)
        auto_name = auto.get("name", "") if auto else None
        items = []
        for d in cands:
            nm = d.get("name", "")
            items.append({"name": nm, "virtual": _is_virtual(nm),
                          "auto": (nm == auto_name)})
        return {"have": True, "items": items, "auto": auto_name,
                "preferred": PREFERRED_OUTPUT}
    finally:
        try:
            p.terminate()
        except Exception:
            pass


def default_output_name():
    """Human name of the current default playback device (for the UI/health)."""
    if not _HAVE:
        return None
    p = _pa.PyAudio()
    try:
        wasapi = p.get_host_api_info_by_type(_pa.paWASAPI)
        return p.get_device_info_by_index(wasapi["defaultOutputDevice"]).get("name")
    except Exception:
        return None
    finally:
        try:
            p.terminate()
        except Exception:
            pass


def _rms16(data):
    """RMS (0..1) of a little-endian 16-bit PCM buffer, cheaply."""
    import array
    a = array.array("h")
    try:
        a.frombytes(data)
    except Exception:
        return 0.0
    if not a:
        return 0.0
    # sample to keep it light on long buffers
    step = max(1, len(a) // 1024)
    acc = 0.0
    n = 0
    for i in range(0, len(a), step):
        v = a[i]
        acc += v * v
        n += 1
    return math.sqrt(acc / n) / 32768.0 if n else 0.0


def _record_loop(wav_path):
    # PREFERRED: capture ONLY FragPunk's audio via Win10 2004+ per-process loopback,
    # so clips don't carry Discord/browser/music. Any failure (old Windows, API error,
    # game not found) falls through to the whole-desktop loopback below -- the recorder
    # can never be broken by this, only upgraded.
    try:
        import fragroute_procaudio as _pca
        if _pca.available():
            _pids = _pca.find_fragpunk_pids()
            if _pids and _pca.capture(_pids, str(wav_path), lambda: _STATE["stop"], _STATE):
                return
    except Exception as _e:
        _STATE["err_proc"] = str(_e)[:140]
    _STATE["mode"] = "system"     # recording the whole default output (all apps)
    p = _pa.PyAudio()
    stream = None
    wf = None
    raw = None
    try:
        dev = _find_loopback(p)
        if not dev:
            _STATE["err"] = "no WASAPI loopback device"
            return
        ch = int(dev.get("maxInputChannels") or 2) or 2
        sr = int(dev.get("defaultSampleRate") or 48000) or 48000
        idx = dev["index"]
        _STATE.update(device=dev.get("name", ""), sr=sr, ch=ch)
        # open the loopback stream; int16 works on shared-mode WASAPI (verified).
        fmt = _pa.paInt16
        convert_float = False
        try:
            stream = p.open(format=fmt, channels=ch, rate=sr, input=True,
                            input_device_index=idx, frames_per_buffer=2048)
        except Exception:
            fmt = _pa.paFloat32          # fall back, convert to int16 on write
            convert_float = True
            stream = p.open(format=fmt, channels=ch, rate=sr, input=True,
                            input_device_index=idx, frames_per_buffer=2048)
        # own the file handle so we can flush() mid-recording -> a rolling-clip save
        # can read recent PCM off disk even while this thread keeps writing.
        raw = open(str(wav_path), "wb")
        wf = wave.open(raw, "wb")
        wf.setnchannels(ch)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        _STATE["started"] = time.time()
        _STATE["frames"] = 0
        import array
        while not _STATE["stop"]:
            try:
                data = stream.read(2048, exception_on_overflow=False)
            except Exception:
                break
            if convert_float:
                fa = array.array("f")
                fa.frombytes(data)
                ia = array.array("h", (max(-32768, min(32767, int(x * 32767))) for x in fa))
                data = ia.tobytes()
            wf.writeframes(data)
            _STATE["frames"] += 1
            if (_STATE["frames"] & 7) == 0:      # ~every 8 buffers
                _STATE["level"] = _rms16(data)
                try:
                    raw.flush()                  # make recent audio visible on disk
                except Exception:
                    pass
    except Exception as e:
        _STATE["err"] = str(e)[:140]
    finally:
        try:
            if stream is not None:
                stream.stop_stream()
                stream.close()
        except Exception:
            pass
        try:
            if wf is not None:
                wf.close()                       # writes the correct WAV header
        except Exception:
            pass
        try:
            if raw is not None and not raw.closed:
                raw.close()
        except Exception:
            pass
        try:
            p.terminate()
        except Exception:
            pass


def start(ring_dir):
    """Begin recording the default-output loopback into ring_dir/audio.wav.
    Returns True if the capture thread started (device present)."""
    if not _HAVE:
        _STATE["err"] = "pyaudiowpatch not installed"
        return False
    with _LOCK:
        t = _STATE["thread"]
        if t is not None and t.is_alive():
            return True
        wav = Path(ring_dir) / "audio.wav"
        try:
            if wav.exists():
                wav.unlink()
        except Exception:
            pass
        _STATE.update(stop=False, wav=str(wav), err="", level=0.0,
                      started=0.0, frames=0)
        th = threading.Thread(target=_record_loop, args=(wav,), daemon=True)
        _STATE["thread"] = th
        th.start()
    # give it a moment to open the device / fail fast
    time.sleep(0.4)
    th = _STATE["thread"]
    ok = bool(th and th.is_alive())
    if not ok and not _STATE.get("err"):
        _STATE["err"] = "loopback capture did not start"
    return ok


def stop():
    """Stop capture, finalize the WAV. Returns the wav path (or None)."""
    with _LOCK:
        _STATE["stop"] = True
        th = _STATE["thread"]
    if th is not None:
        try:
            th.join(timeout=4)
        except Exception:
            pass
    _STATE["thread"] = None
    wav = _STATE.get("wav")
    try:
        if wav and Path(wav).exists() and Path(wav).stat().st_size > 44:
            return wav
    except Exception:
        pass
    return None


def is_recording():
    th = _STATE.get("thread")
    return bool(th is not None and th.is_alive())


def wav_path():
    return _STATE.get("wav")


def wav_duration(path=None):
    """Seconds of audio in the WAV (0 on error)."""
    path = path or _STATE.get("wav")
    if not path:
        return 0.0
    try:
        with wave.open(str(path), "rb") as w:
            return w.getnframes() / float(w.getframerate() or 1)
    except Exception:
        return 0.0


def started_at():
    """Epoch when the capture loop began writing samples (0 if not recording). Used
    to align audio against the video, which starts a moment later."""
    return float(_STATE.get("started", 0) or 0)


def snapshot(out_path, tail_seconds=None, head_seconds=0):
    """Write a proper WAV to out_path from the current capture, reading RAW PCM by
    byte size (so it works whether the source WAV is finalized OR still growing).
    tail_seconds keeps only the last N seconds (rolling-clip save); None keeps all.
    head_seconds DROPS the first N seconds -- used to compensate the audio-vs-video
    startup lead so full-match clips stay in sync. Returns the path, or None."""
    src = _STATE.get("wav")
    sr = int(_STATE.get("sr") or 48000) or 48000
    ch = int(_STATE.get("ch") or 2) or 2
    if not src:
        return None
    try:
        with open(str(src), "rb") as f:
            blob = f.read()
    except Exception:
        return None
    # skip the 44-byte canonical PCM WAV header written by the wave module
    pcm = blob[44:] if len(blob) > 44 else b""
    frame_bytes = 2 * ch
    if len(pcm) < frame_bytes:
        return None
    # align to a full frame
    pcm = pcm[: len(pcm) - (len(pcm) % frame_bytes)]
    if head_seconds and head_seconds > 0:
        skip = int(head_seconds * sr) * frame_bytes
        if 0 < skip < len(pcm):
            pcm = pcm[skip:]
            pcm = pcm[len(pcm) % frame_bytes:]      # re-align after head cut
    if tail_seconds:
        keep = int(tail_seconds) * sr * frame_bytes
        if keep and len(pcm) > keep:
            pcm = pcm[-keep:]
            pcm = pcm[len(pcm) % frame_bytes:]   # re-align after tail cut
    try:
        with wave.open(str(out_path), "wb") as w:
            w.setnchannels(ch)
            w.setsampwidth(2)
            w.setframerate(sr)
            w.writeframes(pcm)
        return str(out_path)
    except Exception:
        return None


def status():
    return {
        "have": _HAVE,
        "recording": is_recording(),
        "device": _STATE.get("device") or default_output_name() or "",
        "level": round(_STATE.get("level", 0.0), 4),
        "sr": _STATE.get("sr", 0),
        "ch": _STATE.get("ch", 0),
        "err": _STATE.get("err", ""),
        "build": APP_AUDIO_BUILD,
    }


def probe(seconds=1.2):
    """Record a short burst from the loopback and return the peak level, so the UI
    can tell the user 'your game audio IS being captured' (or is silent). Does not
    touch the main capture WAV. Safe to call anytime (opens its own device)."""
    if not _HAVE:
        return {"ok": False, "message": "pyaudiowpatch not installed", "level": 0.0}
    p = _pa.PyAudio()
    stream = None
    try:
        dev = _find_loopback(p)
        if not dev:
            return {"ok": False, "message": "no loopback device", "level": 0.0}
        ch = int(dev.get("maxInputChannels") or 2) or 2
        sr = int(dev.get("defaultSampleRate") or 48000) or 48000
        try:
            stream = p.open(format=_pa.paInt16, channels=ch, rate=sr, input=True,
                            input_device_index=dev["index"], frames_per_buffer=2048)
            conv = False
        except Exception:
            stream = p.open(format=_pa.paFloat32, channels=ch, rate=sr, input=True,
                            input_device_index=dev["index"], frames_per_buffer=2048)
            conv = True
        import array
        peak = 0.0
        t0 = time.time()
        while time.time() - t0 < seconds:
            data = stream.read(2048, exception_on_overflow=False)
            if conv:
                fa = array.array("f")
                fa.frombytes(data)
                data = array.array("h", (max(-32768, min(32767, int(x * 32767))) for x in fa)).tobytes()
            peak = max(peak, _rms16(data))
        return {"ok": True, "level": round(peak, 4), "device": dev.get("name", ""),
                "hasSound": peak > 0.004,
                "message": ("Game audio detected" if peak > 0.004
                            else "Silent -- play some sound and re-test")}
    except Exception as e:
        return {"ok": False, "message": str(e)[:120], "level": 0.0}
    finally:
        try:
            if stream is not None:
                stream.stop_stream()
                stream.close()
        except Exception:
            pass
        try:
            p.terminate()
        except Exception:
            pass
