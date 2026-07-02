"""FRAGROUTE voice commands -- talk to the AI hands-free in-game (local STT).

Wraps whisper.cpp (whisper-cli.exe) + a ggml model as a sidecar (the `stt` folder),
and records the mic with the bundled ffmpeg. Flow: hotkey -> record a few seconds ->
whisper transcribes -> the text is fed to the coach -> it answers by voice (backend
SAPI). All local, no internet.

The engine sets STT_DIR + FFMPEG. Pure stdlib (subprocess).
"""
import math
import os
import re
import subprocess
import tempfile
import time
import wave
from pathlib import Path

# pyaudiowpatch gives us real-time mic frames for voice-activity detection (stop
# recording the instant you stop talking). Optional -- falls back to fixed-window
# ffmpeg recording if it's not present.
try:
    import pyaudiowpatch as _pa
except Exception:
    _pa = None

APP_VOICE_BUILD = "voice-2"    # voice-2: VAD (auto-stop on silence) for snappy voice chat

STT_DIR = None             # set by engine; default <module|exe>/stt
FFMPEG = None              # set by engine -> path to ffmpeg.exe (for mic capture)
PREFERRED_MIC = None       # set by engine from the 'voiceMic' setting (name; None=default)
_MIC = {"device": None, "checked": False}
_NOWIN = {"creationflags": 0x08000000} if os.name == "nt" else {}


def _base_dir():
    if STT_DIR:
        return Path(STT_DIR)
    import sys
    base = (Path(sys.executable).parent if getattr(sys, "frozen", False)
            else Path(__file__).parent)
    return base / "stt"


def find_whisper():
    d = _base_dir()
    if d.exists():
        for name in ("whisper-cli.exe", "main.exe"):
            for p in d.rglob(name):
                return str(p)
    return None


def find_model():
    """Pick the MOST capable whisper model present, for the best freeform-speech
    accuracy: large > medium > small > base > tiny. Drop a bigger ggml-*.bin in the
    stt folder and it's used automatically."""
    d = _base_dir()
    if not d.exists():
        return None
    bins = list(d.glob("*.bin"))
    if not bins:
        return None
    order = ["large", "medium", "small", "base", "tiny"]

    def rank(p):
        n = p.name.lower()
        for i, k in enumerate(order):
            if k in n:
                return i
        return len(order)
    bins.sort(key=rank)
    return str(bins[0])


def available():
    # Whisper (the STT binary + a model) is the real requirement. Audio capture can
    # come from pyaudio VAD (no ffmpeg needed) OR ffmpeg -- so don't report voice as
    # 'missing' just because the ffmpeg path wasn't wired. This was showing a false
    # "voice commands / whisper missing" in Setup even with whisper fully installed.
    if not (find_whisper() and find_model()):
        return False
    return bool(FFMPEG or vad_available())


def detect_mic(refresh=False):
    """Find the default microphone's dshow device name (cached)."""
    if _MIC["checked"] and not refresh:
        return _MIC["device"]
    _MIC["checked"] = True
    if not FFMPEG:
        return None
    try:
        p = subprocess.run([FFMPEG, "-hide_banner", "-list_devices", "true",
                           "-f", "dshow", "-i", "dummy"],
                          capture_output=True, text=True, timeout=12, **_NOWIN)
        for line in (p.stderr or "").splitlines():
            m = re.search(r'"([^"]+)"\s*\(audio\)', line)
            if m:
                _MIC["device"] = m.group(1)
                break
    except Exception:
        pass
    return _MIC["device"]


def record(seconds=5):
    """Record the mic to a 16kHz mono WAV (what whisper wants). Returns path or None."""
    mic = PREFERRED_MIC or detect_mic()      # user-selected mic (or auto default)
    if not mic or not FFMPEG:
        return None
    wav = os.path.join(tempfile.gettempdir(), "fragroute_voice.wav")
    # a modest gain boost helps whisper hear a quiet mic without clipping speech;
    # 16kHz mono is what whisper wants.
    # highpass kills rumble; dynaudnorm auto-boosts a quiet mic to a consistent level
    # so whisper reliably hears you even if your input gain is low. 16kHz mono for whisper.
    args = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
            "-f", "dshow", "-i", "audio=" + mic, "-t", str(int(seconds)),
            "-af", "highpass=f=90,speechnorm=e=50:r=0.0001:l=1", "-ar", "16000", "-ac", "1", wav]
    try:
        subprocess.run(args, timeout=int(seconds) + 12, **_NOWIN)
        return wav if (os.path.exists(wav) and os.path.getsize(wav) > 0) else None
    except Exception:
        return None


def vad_available():
    return _pa is not None


def list_mics():
    """Enumerate usable microphone INPUT devices so the user can pick which one the
    coach listens to (fixes 'the AI can't hear my mic' when the default input is the
    wrong device). Returns [{name, default}] with a 'System default' entry first.
    Deduped by name; loopback/output devices excluded."""
    out = [{"name": "System default", "default": True, "system": True}]
    if _pa is None:
        return out
    p = _pa.PyAudio()
    try:
        try:
            dflt = p.get_default_input_device_info().get("name")
        except Exception:
            dflt = None
        # Prefer the WASAPI host API: full, un-truncated device names and no MME/
        # DirectSound duplicates or aggregate pseudo-devices ("Sound Mapper" etc.).
        wasapi_idx = None
        try:
            wasapi_idx = p.get_host_api_info_by_type(_pa.paWASAPI).get("index")
        except Exception:
            wasapi_idx = None
        # generic aggregate inputs that aren't a real mic -- never useful to pick
        _JUNK = ("sound mapper", "primary sound capture", "@system32")
        for pass_wasapi in (True, False):          # WASAPI-only first; fall back to all
            if not (pass_wasapi and wasapi_idx is None) and len(out) > 1:
                break                              # got WASAPI devices -> don't add dupes
            seen = set(m["name"] for m in out)
            for i in range(p.get_device_count()):
                try:
                    d = p.get_device_info_by_index(i)
                except Exception:
                    continue
                if int(d.get("maxInputChannels") or 0) <= 0 or d.get("isLoopbackDevice"):
                    continue
                if pass_wasapi and wasapi_idx is not None and d.get("hostApi") != wasapi_idx:
                    continue
                name = (d.get("name") or "").strip()
                low = name.lower()
                if not name or name in seen or any(j in low for j in _JUNK):
                    continue
                seen.add(name)
                out.append({"name": name, "default": (name == dflt), "system": False})
    finally:
        try:
            p.terminate()
        except Exception:
            pass
    return out


def _resolve_input_index(p, mic_name):
    """pyaudio input-device index for a chosen mic NAME (None/'' -> system default).

    Prefers the WASAPI backend of a device: Windows' raw default input is often the
    MME endpoint, which on some mics (e.g. a Yeti) OPENS but returns silence at 16k
    mono -> the coach 'hears nothing'. The WASAPI version of the SAME mic works. So
    even for the default we hunt down its WASAPI twin."""
    try:
        wasapi_idx = p.get_host_api_info_by_type(_pa.paWASAPI).get("index")
    except Exception:
        wasapi_idx = None

    def _ok(d):
        return int(d.get("maxInputChannels") or 0) > 0 and not d.get("isLoopbackDevice")

    if not mic_name:
        # match the default device's base name, preferring its WASAPI twin
        try:
            base = (p.get_default_input_device_info().get("name") or "").split(" (")[0]
        except Exception:
            base = ""
        best = None
        for i in range(p.get_device_count()):
            try:
                d = p.get_device_info_by_index(i)
            except Exception:
                continue
            if not _ok(d):
                continue
            nm = d.get("name") or ""
            if base and base in nm:
                if wasapi_idx is not None and d.get("hostApi") == wasapi_idx:
                    return i                       # WASAPI twin of the default -> best
                if best is None:
                    best = i
        if best is not None:
            return best
        try:
            return p.get_default_input_device_info().get("index")
        except Exception:
            return None

    # explicit name: exact match, then WASAPI substring, then any substring
    exact = wasapi_sub = any_sub = None
    ml = mic_name.lower()
    for i in range(p.get_device_count()):
        try:
            d = p.get_device_info_by_index(i)
        except Exception:
            continue
        if not _ok(d):
            continue
        nm = (d.get("name") or "")
        is_wasapi = (wasapi_idx is not None and d.get("hostApi") == wasapi_idx)
        if nm == mic_name and (exact is None or is_wasapi):
            exact = i
        elif ml in nm.lower():
            if is_wasapi and wasapi_sub is None:
                wasapi_sub = i
            elif any_sub is None:
                any_sub = i
    for pick in (exact, wasapi_sub, any_sub):
        if pick is not None:
            return pick
    try:
        return p.get_default_input_device_info().get("index")
    except Exception:
        return None


def _open_input(p, idx, frames_per_buffer=1024):
    """Open an input device, trying its NATIVE rate/channels first so we don't hit
    paInvalidSampleRate (error -9997) forcing 16k on a 44.1k-only mic like a Yeti.
    Returns (stream, rate, channels) or (None, 0, 0)."""
    try:
        d = p.get_device_info_by_index(idx) if idx is not None else {}
    except Exception:
        d = {}
    native_sr = int(d.get("defaultSampleRate") or 44100) or 44100
    native_ch = min(2, int(d.get("maxInputChannels") or 1)) or 1
    # native first, then common safe combos
    attempts = [(native_sr, native_ch), (44100, 1), (48000, 1), (16000, 1),
                (44100, 2), (48000, 2)]
    seen = set()
    for sr, ch in attempts:
        key = (sr, ch)
        if key in seen:
            continue
        seen.add(key)
        try:
            s = p.open(format=_pa.paInt16, channels=ch, rate=sr, input=True,
                       input_device_index=idx, frames_per_buffer=frames_per_buffer)
            return s, sr, ch
        except Exception:
            continue
    return None, 0, 0


def mic_probe(mic_name=None, seconds=1.4):
    """Record a short burst from the chosen mic and return the peak level, so the UI
    can tell the user 'this mic hears you' (or is silent -> pick another)."""
    name = mic_name if mic_name is not None else PREFERRED_MIC
    if _pa is None:
        return {"ok": False, "message": "mic testing needs the voice module", "level": 0.0}
    p = _pa.PyAudio()
    stream = None
    try:
        idx = _resolve_input_index(p, name)
        try:
            actual = p.get_device_info_by_index(idx).get("name") if idx is not None else "default"
        except Exception:
            actual = "default"
        stream, sr, ch = _open_input(p, idx)
        if stream is None:
            return {"ok": False, "message": "couldn't open that mic (unsupported format)", "level": 0.0}
        peak = 0.0
        t0 = time.time()
        while time.time() - t0 < seconds:
            try:
                peak = max(peak, _rms16_norm(stream.read(1024, exception_on_overflow=False)))
            except Exception:
                break
        # the app auto-boosts quiet mics (speechnorm), so even a low raw level is
        # usable -- but a near-silent read means the signal isn't reaching the mic.
        heard = peak > 0.015
        usable = peak > 0.004
        if heard:
            msg = "Heard you clearly."
        elif usable:
            msg = "Faint but usable -- the app will boost it. For best results turn up your mic gain."
        else:
            msg = ("Almost no signal. On a Blue Yeti: turn UP the gain knob on the BACK, "
                   "make sure the top MUTE button is solid (not flashing), and set the mic "
                   "level near 100 in Windows Sound settings.")
        return {"ok": True, "level": round(peak, 4), "device": actual,
                "heard": heard, "usable": usable, "message": msg}
    finally:
        try:
            if stream is not None:
                stream.stop_stream(); stream.close()
        except Exception:
            pass
        try:
            p.terminate()
        except Exception:
            pass


def _rms16_norm(data):
    import array
    a = array.array("h")
    try:
        a.frombytes(data)
    except Exception:
        return 0.0
    if not a:
        return 0.0
    acc = 0.0
    for v in a:
        acc += v * v
    return math.sqrt(acc / len(a)) / 32768.0


def record_vad(max_seconds=12, start_timeout=5.0, silence_hang=0.8, min_speech=0.25):
    """Record the mic with VOICE-ACTIVITY DETECTION: wait for you to start talking,
    then stop ~`silence_hang`s after you stop -- so a short reply returns in ~1-2s
    instead of always waiting a fixed window. Writes a 16kHz mono WAV (post-processed
    through the same highpass/dynaudnorm/gain as record() so whisper hears it well).

    Returns the wav path, or None if nothing was said. Falls back to a fixed-window
    record() when pyaudio isn't available."""
    if _pa is None:
        return record(int(max_seconds))
    chunk = 1024
    raw_path = os.path.join(tempfile.gettempdir(), "fragroute_voice_raw.wav")
    p = _pa.PyAudio()
    stream = None
    try:
        idx = _resolve_input_index(p, PREFERRED_MIC)   # user-selected mic (or default)
        # open at the device's NATIVE rate/channels (avoids paInvalidSampleRate -9997
        # on a 44.1k-only mic); ffmpeg down-mixes to 16k mono for whisper on save.
        stream, rate, ch = _open_input(p, idx, frames_per_buffer=chunk)
        if stream is None:
            return record(int(max_seconds))       # can't open -> proven ffmpeg path
        # 1) calibrate the noise floor from the first ~250ms
        base = []
        for _ in range(max(1, int(0.25 * rate / chunk))):
            try:
                base.append(_rms16_norm(stream.read(chunk, exception_on_overflow=False)))
            except Exception:
                break
        floor = (sorted(base)[len(base) // 2] if base else 0.0)
        # threshold above the floor, but CAPPED so that if you happen to be talking
        # during the 250ms calibration (floor reads high) it can't set an impossibly
        # high bar that your normal speech never clears.
        thr = min(0.045, max(0.010, floor * 2.5 + 0.006))
        frames = []
        started = False
        t0 = time.time()
        last_voice = t0
        speech_frames = 0
        while True:
            try:
                data = stream.read(chunk, exception_on_overflow=False)
            except Exception:
                break
            now = time.time()
            lvl = _rms16_norm(data)
            if not started:
                if lvl >= thr:
                    started = True
                    last_voice = now
                    frames.append(data)
                    speech_frames += 1
                elif now - t0 > start_timeout:
                    break                          # nobody spoke -> give up
                continue
            frames.append(data)
            if lvl >= thr:
                last_voice = now
                speech_frames += 1
            # stop conditions: trailing silence, or hit the max length
            if now - last_voice >= silence_hang:
                break
            if now - t0 >= max_seconds:
                break
        if not started or (speech_frames * chunk / float(rate)) < min_speech:
            return None                            # nothing meaningful captured
        try:
            with wave.open(raw_path, "wb") as w:
                w.setnchannels(ch)
                w.setsampwidth(2)
                w.setframerate(rate)
                w.writeframes(b"".join(frames))
        except Exception:
            return None
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
    # 2) clean it up through the same filter chain record() uses (whisper likes it)
    out = os.path.join(tempfile.gettempdir(), "fragroute_voice.wav")
    if FFMPEG:
        try:
            subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-i", raw_path,
                            "-af", "highpass=f=90,speechnorm=e=50:r=0.0001:l=1",
                            "-ar", "16000", "-ac", "1", out], timeout=20, **_NOWIN)
            if os.path.exists(out) and os.path.getsize(out) > 0:
                return out
        except Exception:
            pass
    return raw_path if os.path.exists(raw_path) else None


def transcribe(wav):
    """Run whisper-cli on a WAV; return the transcribed text (from stdout)."""
    w, m = find_whisper(), find_model()
    if not w or not m or not wav or not os.path.exists(wav):
        return None
    # A game-vocabulary initial prompt biases whisper toward FragPunk/shooter terms
    # so freeform, casual, in-game speech transcribes far more accurately. More
    # threads (this rig has plenty) keeps it snappy.
    prompt = ("FragPunk shooter. Lancers, shard cards, weapons, crosshair, aim, peek, "
              "rotate, push, queue, region, ping, clutch, headshot, entry, retake, flank.")
    # -ng (no GPU): run on the CPU, NOT the 4070. Whisper on the game GPU stutters
    # your FPS; this 24-core Ryzen transcribes small.en in a couple seconds with zero
    # game-GPU impact. -t 6 leaves plenty of cores for the game.
    args = [w, "-m", m, "-f", wav, "-nt", "-l", "en", "-ng", "-t", "6",
            "--beam-size", "5", "--prompt", prompt]
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=90, **_NOWIN)
        text = (p.stdout or "").strip()
        # strip any leading [bracketed] tokens whisper sometimes emits
        text = re.sub(r"^\[[^\]]*\]\s*", "", text).strip()
        return text or None
    except Exception:
        return None


def listen(seconds=5):
    """Record + transcribe in one call. Returns the spoken text or None."""
    wav = record(seconds)
    if not wav:
        return None
    return transcribe(wav)


def status():
    return {"available": available(), "mic": _MIC.get("device"),
            "model": Path(find_model()).name if find_model() else None}
