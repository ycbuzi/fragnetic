"""FRAGROUTE voice commands -- talk to the AI hands-free in-game (local STT).

Wraps whisper.cpp (whisper-cli.exe) + a ggml model as a sidecar (the `stt` folder),
and records the mic with the bundled ffmpeg. Flow: hotkey -> record a few seconds ->
whisper transcribes -> the text is fed to the coach -> it answers by voice (backend
SAPI). All local, no internet.

The engine sets STT_DIR + FFMPEG. Pure stdlib (subprocess).
"""
import os
import re
import subprocess
import tempfile
from pathlib import Path

APP_VOICE_BUILD = "voice-1"

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
    args = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
            "-f", "dshow", "-i", "audio=" + mic, "-t", str(int(seconds)),
            "-af", "volume=4", "-ar", "16000", "-ac", "1", wav]
    try:
        subprocess.run(args, timeout=int(seconds) + 12, **_NOWIN)
        return wav if (os.path.exists(wav) and os.path.getsize(wav) > 0) else None
    except Exception:
        return None


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
    args = [w, "-m", m, "-f", wav, "-nt", "-l", "en", "-t", "8",
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
