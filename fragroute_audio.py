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

APP_AUDIO_BUILD = "audio-1"

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


def _find_loopback(p):
    """The loopback device for the current DEFAULT output. Falls back to any
    loopback device if the default can't be matched by name."""
    try:
        wasapi = p.get_host_api_info_by_type(_pa.paWASAPI)
        dflt = p.get_device_info_by_index(wasapi["defaultOutputDevice"])
        name = dflt.get("name", "")
    except Exception:
        name = ""
    # 1) exact: a loopback device whose name contains the default output's name
    if name:
        for i in range(p.get_device_count()):
            try:
                d = p.get_device_info_by_index(i)
            except Exception:
                continue
            if d.get("isLoopbackDevice") and name.split(" (")[0] in d.get("name", ""):
                return d
    # 2) any loopback device
    try:
        for d in p.get_loopback_device_info_generator():
            return d
    except Exception:
        pass
    return None


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


def snapshot(out_path, tail_seconds=None):
    """Write a proper WAV to out_path from the current capture, reading RAW PCM by
    byte size (so it works whether the source WAV is finalized OR still growing).
    tail_seconds keeps only the last N seconds (for a rolling-clip save); None keeps
    everything. Returns the written path, or None on failure/empty."""
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
