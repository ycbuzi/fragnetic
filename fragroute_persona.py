"""Per-user adaptive coach personality.

Every player gets their OWN coach vibe that LEARNS from how they respond: warm vs
blunt, hype vs calm, detailed vs concise, casual vs pro. It reads each message the
user sends (length, slang, frustration, excitement, curiosity) plus explicit
feedback (thumbs, "be more X") and nudges the traits, then turns them into a short
style instruction injected into the coach's system prompt. Persists per account.

Traits are 0..1. Storage: fragroute_persona.json keyed by (lowercased) username.
"""
import json
import re
import threading
from pathlib import Path

APP_PERSONA_BUILD = "persona-1"

BASE_DIR = None
_LOCK = threading.Lock()

TRAITS = ("warmth", "energy", "detail", "casual")
DEFAULT = {"warmth": 0.6, "energy": 0.5, "detail": 0.5, "casual": 0.6, "n": 0}

# preset base personalities the user (or first-run) can pick
PRESETS = {
    "hype":    {"warmth": 0.8, "energy": 0.9, "detail": 0.4, "casual": 0.8},
    "analyst": {"warmth": 0.55, "energy": 0.35, "detail": 0.8, "casual": 0.35},
    "drill":   {"warmth": 0.3, "energy": 0.6, "detail": 0.5, "casual": 0.4},
    "friend":  {"warmth": 0.8, "energy": 0.5, "detail": 0.45, "casual": 0.85},
    "soothing": {"warmth": 0.85, "energy": 0.3, "detail": 0.55, "casual": 0.6},
}

_FRUSTRATION = ("tilt", "tilted", "trash", "garbage", "wtf", "hate", "ugh", "sucks",
                "worst", "rage", "annoy", "frustrat", "lag", "unfair", "bs")
_EXCITED = ("lets go", "let's go", "lfg", "clutch", "insane", "cracked", "poppin",
            "ez", "gg", "sheesh", "!!!", "pog", "goated")
_CASUAL = ("lol", "lmao", "bruh", "bro", "haha", "yo ", "nah", "idk", "tbh", "ngl", "fr ")


def _path():
    root = Path(BASE_DIR) if BASE_DIR else Path(__file__).parent
    return root / "fragroute_persona.json"


def _load():
    try:
        return json.loads(_path().read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(d):
    try:
        _path().write_text(json.dumps(d, indent=2), encoding="utf-8")
    except Exception:
        pass


def _key(user):
    return (user or "default").strip().lower() or "default"


def _clamp(x):
    return max(0.0, min(1.0, x))


def profile(user):
    with _LOCK:
        d = _load()
        p = d.get(_key(user))
        if not p:
            p = dict(DEFAULT)
            d[_key(user)] = p
            _save(d)
        return dict(p)


def set_base(user, preset):
    """Set the starting personality from a named preset."""
    base = PRESETS.get(preset)
    if not base:
        return profile(user)
    with _LOCK:
        d = _load()
        p = dict(DEFAULT)
        p.update(base)
        p["n"] = 0
        p["preset"] = preset
        d[_key(user)] = p
        _save(d)
        return dict(p)


def nudge(user, trait, delta):
    """Explicit adjustment (e.g. 'be more blunt' -> warmth -0.15)."""
    if trait not in TRAITS:
        return profile(user)
    with _LOCK:
        d = _load()
        p = d.get(_key(user)) or dict(DEFAULT)
        p[trait] = _clamp(float(p.get(trait, 0.5)) + float(delta))
        d[_key(user)] = p
        _save(d)
        return dict(p)


def observe(user, text, reaction=None):
    """Learn from a user message + optional reaction ('up'/'down'). Small nudges so
    the style drifts gradually toward what this player responds to. Never raises."""
    try:
        t = (text or "").strip()
        low = t.lower()
        with _LOCK:
            d = _load()
            p = d.get(_key(user)) or dict(DEFAULT)

            def bump(trait, dv):
                p[trait] = _clamp(float(p.get(trait, 0.5)) + dv)

            words = len(t.split())
            if words:
                # message length -> preferred detail level
                if words <= 5:
                    bump("detail", -0.03)
                elif words >= 25:
                    bump("detail", +0.03)
                # slang / lowercase / no punctuation -> casual
                if any(k in low for k in _CASUAL) or (t == low and not re.search(r"[.?!]", t)):
                    bump("casual", +0.04)
                # frustration -> be warmer + calmer (support, don't pile on)
                if any(k in low for k in _FRUSTRATION):
                    bump("warmth", +0.06)
                    bump("energy", -0.04)
                # excitement / caps -> match their energy
                caps = sum(1 for c in t if c.isupper())
                if any(k in low for k in _EXCITED) or (len(t) > 6 and caps / max(1, len(t)) > 0.5):
                    bump("energy", +0.06)
                # curiosity (why/how/what if) -> more detail
                if re.search(r"\b(why|how come|how do|explain|what if|breakdown|break down)\b", low):
                    bump("detail", +0.05)

            if reaction == "up":            # they liked the last reply -> reinforce nothing (stable)
                p["nGood"] = int(p.get("nGood", 0)) + 1
            elif reaction == "down":        # disliked -> shake the two least-committed traits toward middle
                for tr in TRAITS:
                    p[tr] = _clamp(float(p.get(tr, 0.5)) + (0.05 if p.get(tr, 0.5) < 0.5 else -0.05))

            p["n"] = int(p.get("n", 0)) + 1
            d[_key(user)] = p
            _save(d)
            return dict(p)
    except Exception:
        return profile(user)


def _lvl(v, lo, mid, hi):
    return lo if v < 0.34 else (hi if v > 0.66 else mid)


def persona_prompt(user):
    """Turn the learned traits into a short natural-language style instruction that
    prepends the coach's system prompt."""
    p = profile(user)
    parts = []
    parts.append(_lvl(p["warmth"],
                      "Be blunt and direct -- straight, honest feedback, minimal softening.",
                      "Be encouraging but honest.",
                      "Be warm, supportive, and reassuring; lead with what went well."))
    parts.append(_lvl(p["energy"],
                      "Keep a calm, measured, even tone.",
                      "Keep a steady, upbeat tone.",
                      "Bring high energy -- hype the good plays."))
    parts.append(_lvl(p["detail"],
                      "Keep replies short and punchy (1-2 sentences).",
                      "Give a focused answer with one concrete tip.",
                      "Give a thorough breakdown with the reasoning."))
    parts.append(_lvl(p["casual"],
                      "Speak professionally, like an esports analyst.",
                      "Speak naturally.",
                      "Talk casually, like a friend who's good at the game (light slang ok)."))
    return "COACHING STYLE for this player (adapt to it): " + " ".join(parts)


def status(user):
    p = profile(user)
    return {"build": APP_PERSONA_BUILD, "user": _key(user), "traits": {t: round(p.get(t, 0.5), 2) for t in TRAITS},
            "interactions": p.get("n", 0), "preset": p.get("preset"),
            "style": persona_prompt(user), "presets": list(PRESETS.keys())}
