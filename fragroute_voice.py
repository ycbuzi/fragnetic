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
    return bool(find_whisper() and find_model() and FFMPEG)


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
    mic = detect_mic()
    if not mic or not FFMPEG:
        return None
    wav = os.path.join(tempfile.gettempdir(), "fragroute_voice.wav")
    # a modest gain boost helps whisper hear a quiet mic without clipping speech;
    # 16kHz mono is what whisper wants.
    # highpass kills rumble; dynaudnorm auto-boosts a quiet mic to a consistent level
    # so whisper reliably hears you even if your input gain is low. 16kHz mono for whisper.
    args = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
            "-f", "dshow", "-i", "audio=" + mic, "-t", str(int(seconds)),
            "-af", "highpass=f=90,dynaudnorm=p=0.9:m=12,volume=2", "-ar", "16000", "-ac", "1", wav]
    try:
        subprocess.run(args, timeout=int(seconds) + 12, **_NOWIN)
        return wav if (os.path.exists(wav) and os.path.getsize(wav) > 0) else None
    except Exception:
        return None


def vad_available():
    return _pa is not None


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
    rate = 16000
    chunk = 512                       # 32ms frames at 16kHz -> responsive VAD
    raw_path = os.path.join(tempfile.gettempdir(), "fragroute_voice_raw.wav")
    p = _pa.PyAudio()
    stream = None
    try:
        try:
            idx = p.get_default_input_device_info().get("index")
        except Exception:
            idx = None
        try:
            stream = p.open(format=_pa.paInt16, channels=1, rate=rate, input=True,
                            input_device_index=idx, frames_per_buffer=chunk)
        except Exception:
            return record(int(max_seconds))       # device won't open at 16k mono -> ffmpeg path
        # 1) calibrate the noise floor from the first ~250ms
        base = []
        for _ in range(max(1, int(0.25 * rate / chunk))):
            try:
                base.append(_rms16_norm(stream.read(chunk, exception_on_overflow=False)))
            except Exception:
                break
        floor = (sorted(base)[len(base) // 2] if base else 0.0)
        thr = max(0.012, floor * 2.5 + 0.006)     # speech threshold above the floor
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
                w.setnchannels(1)
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
                            "-af", "highpass=f=90,dynaudnorm=p=0.9:m=12,volume=2",
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
