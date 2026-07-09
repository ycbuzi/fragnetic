"""Fragnetic neural TTS -- the coach's VOICE.

Uses Piper (offline neural TTS) with a warm/soothing voice model. Much better than
Windows SAPI (David/Zira). Falls back to SAPI in the engine if Piper isn't present.
All local, no internet. The engine sets TTS_DIR to the 'tts' sidecar folder.
"""
import os
import subprocess
import tempfile
import threading
from pathlib import Path

import fragroute_proc as _proc   # orphan-proof helpers (shared Windows Job Object)

APP_TTS_BUILD = "tts-2"          # tts-2: job-adopt piper/ffplay so a callout can't orphan

TTS_DIR = None
_NOWIN = {}
if os.name == "nt":
    _si = subprocess.STARTUPINFO()
    _si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    _NOWIN = {"startupinfo": _si, "creationflags": 0x08000000}

_SPEAK_LOCK = threading.Lock()


def _base():
    if TTS_DIR:
        return Path(TTS_DIR)
    import sys
    root = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent
    return root / "tts"


def find_piper():
    b = _base()
    for p in (b / "piper" / "piper.exe", b / "piper.exe"):
        if p.exists():
            return str(p)
    return None


def list_voices():
    vd = _base() / "voices"
    if not vd.exists():
        return []
    return sorted(p.name for p in vd.glob("*.onnx"))


def find_voice(name=None):
    vd = _base() / "voices"
    if not vd.exists():
        return None
    if name:
        p = vd / name
        if p.exists():
            return str(p)
    onnx = sorted(vd.glob("*.onnx"))
    return str(onnx[0]) if onnx else None


def available():
    return bool(find_piper() and find_voice())


def synth(text, out_wav, voice=None, rate=None):
    """Render text -> wav via Piper. rate: length_scale (1.0 normal; >1 slower/calmer)."""
    piper = find_piper()
    v = find_voice(voice)
    if not piper or not v or not (text or "").strip():
        return False
    args = [piper, "-m", v, "-f", str(out_wav)]
    if rate:
        args += ["--length_scale", str(rate)]
    try:
        # _proc.run job-adopts the piper child so it can't orphan if we're hard-killed
        # while it's rendering (blocking run alone would leave it running).
        _proc.run(args, input=(text or "").encode("utf-8", "ignore"),
                  capture_output=True, timeout=60, **_NOWIN)
        return Path(out_wav).exists() and Path(out_wav).stat().st_size > 1000
    except Exception:
        return False


def speak(text, voice=None, rate=None):
    """Synthesize + play the coach's voice. Serialized so callouts don't overlap."""
    if not available():
        return False
    wav = os.path.join(tempfile.gettempdir(), "fragnetic_tts.wav")
    with _SPEAK_LOCK:
        if not synth(text, wav, voice, rate):
            return False
        try:
            import winsound
            winsound.PlaySound(wav, winsound.SND_FILENAME)
            return True
        except Exception:
            try:
                _proc.adopt(subprocess.Popen(["ffplay", "-nodisp", "-autoexit", wav], **_NOWIN))
                return True
            except Exception:
                return False


def status():
    return {"build": APP_TTS_BUILD, "available": available(),
            "piper": bool(find_piper()), "voice": (Path(find_voice()).name if find_voice() else None),
            "voices": list_voices()}
