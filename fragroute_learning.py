"""FRAGROUTE learning store -- the self-learning, mode-aware knowledge base.

Per game mode it merges THREE layers (trust order: observed > official > wiki > creator):
  1. seed     -- priors from fragroute_modes (useful on day one, never blank)
  2. observed -- evidence counted from the USER's OWN matches (ground truth)
  3. online   -- facts fetched from FragPunk-only sources, with provenance+date

`profile()` returns the merged EFFECTIVE truth: observed evidence overrides the
seed once confident (e.g. enough respawns seen -> respawns=True). The local LLM
(later) only EXPLAINS this store; it never invents rules. Portable JSON, atomic
writes, stdlib only. The engine sets LEARNING_PATH and calls observe_*(); the AI
coach reads profile()/summary().
"""
import json
import re
import threading
import time
from pathlib import Path

try:
    import fragroute_modes
except Exception:
    fragroute_modes = None

LEARNING_PATH = None            # set by fragroute.main()
# RLock (not Lock): writers hold _LOCK and call load() inside it, so load() must be able
# to (re-)acquire the SAME lock on the same thread without deadlocking.
_LOCK = threading.RLock()
_CACHE = {"loaded": False, "data": None, "saveErr": None}

# how much observed evidence flips a boolean away from the seed
_CONFIRM = 3                    # N consistent observations -> trust it
_DUR_KEEP = 50                 # cap stored match durations per mode


_SCHEMA_VERSION = 2   # bump when the learning schema changes; _migrate() backfills old data


def _blank():
    return {"version": _SCHEMA_VERSION, "updated": 0, "modes": {}}


def _mode_entry(key):
    seed = fragroute_modes.profile_for(key) if fragroute_modes else {}
    return {
        "seed": seed,
        "observed": {
            "matches": 0, "wins": 0, "losses": 0, "durations": [],
            # raw event tallies (populated once the CV detector lands)
            "respawn_seen": 0, "revive_seen": 0, "down_seen": 0,
            "kill_seen": 0, "death_seen": 0, "lancers": {},
        },
        "online": [],           # [{fact, source, trust, date}]
    }


def _migrate(data):
    """Forward-compatible migration: fill any keys added in newer schema versions
    WITHOUT dropping the customer's learned values. This is what keeps years of a
    player's learned data intact across app updates -- old installs upgrade cleanly
    instead of silently losing their coach's memory."""
    if not isinstance(data, dict):
        return _blank()
    data.setdefault("version", 1)
    data.setdefault("updated", 0)
    data.setdefault("modes", {})
    for key, entry in list(data["modes"].items()):
        if not isinstance(entry, dict):
            data["modes"][key] = _mode_entry(key)
            continue
        tmpl = _mode_entry(key)
        entry.setdefault("seed", tmpl["seed"])
        entry.setdefault("online", tmpl["online"])
        obs = entry.setdefault("observed", {})
        for k, v in tmpl["observed"].items():          # backfill fields added later
            if k not in obs:
                obs[k] = dict(v) if isinstance(v, dict) else (list(v) if isinstance(v, list) else v)
    data["version"] = _SCHEMA_VERSION
    return data


def load():
    if _CACHE["loaded"] and _CACHE["data"] is not None:
        return _CACHE["data"]
    with _LOCK:                       # serialize the cache-populate; RLock => writers re-enter safely
        if _CACHE["loaded"] and _CACHE["data"] is not None:
            return _CACHE["data"]      # another thread populated it while we waited on the lock
        data = _blank()
        try:
            if LEARNING_PATH and Path(LEARNING_PATH).exists():
                d = json.loads(Path(LEARNING_PATH).read_text(encoding="utf-8"))
                if isinstance(d, dict) and "modes" in d:
                    data = _migrate(d)
        except Exception:
            pass
        _CACHE["data"] = data
        _CACHE["loaded"] = True
        return data


def _inc(d, key, by=1):
    """Increment a persisted counter defensively. A corrupted/hand-edited learning file
    can hold a non-int (str/null) where an int is expected; a bare `+= 1` would raise
    TypeError and abort the whole observe_*() call. Coerce, and reset to `by` if unusable."""
    try:
        d[key] = int(d.get(key, 0)) + by
    except (TypeError, ValueError):
        d[key] = by


def _save(data):
    data["updated"] = int(time.time() * 1000)
    _CACHE["data"] = data
    if not LEARNING_PATH:
        return
    tmp = str(LEARNING_PATH) + ".tmp"
    try:
        Path(tmp).write_text(json.dumps(data, indent=2), encoding="utf-8")
        Path(tmp).replace(LEARNING_PATH)
        _CACHE["saveErr"] = None
    except Exception as e:
        # Must NOT crash the engine over a telemetry write -- but don't swallow it whole
        # either: record it (Health/diag can surface "learning not persisting") and drop
        # the half-written tmp so it can't masquerade as good data.
        _CACHE["saveErr"] = str(e)
        try:
            Path(tmp).unlink()
        except Exception:
            pass


def _ensure(data, key):
    if key not in data["modes"]:
        data["modes"][key] = _mode_entry(key)
    return data["modes"][key]


# ---- writers (called from the engine event detection) ---------------------
def observe_match(mode_key, outcome=None, durationS=None, lancer=None):
    """Record one completed match for a mode: count it, track win/loss, duration,
    and which Lancer was used. The cheap signals we already have today."""
    if not mode_key:
        mode_key = "unknown"
    with _LOCK:
        data = load()
        m = _ensure(data, mode_key)["observed"]
        _inc(m, "matches")
        o = str(outcome or "").lower()
        if o in ("win", "won"):
            _inc(m, "wins")
        elif o in ("loss", "lost", "lose"):
            _inc(m, "losses")
        if isinstance(durationS, (int, float)) and durationS > 0:
            m["durations"].append(int(durationS))
            del m["durations"][:-_DUR_KEEP]
        if lancer:
            _inc(m["lancers"], lancer)
        _save(data)
    return True


def observe_event(mode_key, kind):
    """Tally a raw in-match event (respawn/revive/down/kill/death). Wired once the
    CV detector exists; harmless no-op-ish counter until then."""
    field = {"respawn": "respawn_seen", "revive": "revive_seen",
             "down": "down_seen", "kill": "kill_seen", "death": "death_seen"}.get(kind)
    if not field:
        return False
    with _LOCK:
        data = load()
        m = _ensure(data, mode_key or "unknown")["observed"]
        _inc(m, field)
        _save(data)
    return True


def record_online_fact(mode_key, fact, source, trust="wiki", date=None):
    """Add a fact learned from a FragPunk-only online source, with provenance.
    Deduped by (source, fact). Caller MUST treat fetched text as data, not commands."""
    fact = (fact or "").strip()
    if not fact:
        return False
    with _LOCK:
        data = load()
        e = _ensure(data, mode_key or "unknown")
        for f in e["online"]:
            if f.get("fact") == fact and f.get("source") == source:
                f["date"] = date or f.get("date")
                _save(data)
                return False          # already present -- not a NEW fact
        e["online"].append({"fact": fact[:400], "source": source,
                            "trust": trust, "date": date or int(time.time() * 1000)})
        e["online"] = e["online"][-60:]
        _save(data)
    return True                        # newly added


# ---- readers (used by the AI coach + /api/learning) -----------------------
def _confident_bool(seed_val, seen, opposite_seen=0):
    """Override a seed boolean only with enough one-sided observed evidence."""
    if seen >= _CONFIRM and seen > opposite_seen:
        return True
    if seed_val:
        return True
    return bool(seed_val)


def profile(mode_key):
    """Merged EFFECTIVE profile: seed, refined by confident observations, with the
    observed stats and online facts attached for the coach to cite."""
    data = load()
    key = mode_key or "unknown"
    seed = (fragroute_modes.profile_for(key) if fragroute_modes else {}) or {}
    entry = data["modes"].get(key)
    eff = dict(seed)
    eff["key"] = key
    obs = (entry or {})
    o = obs.get("observed", {}) if entry else {}
    # observed refinements (only where we have real evidence)
    if o.get("respawn_seen", 0) >= _CONFIRM:
        eff["respawns"] = True
    if o.get("revive_seen", 0) >= 1:
        eff["revive_possible"] = True
    eff["_observed"] = {
        "matches": o.get("matches", 0),
        "wins": o.get("wins", 0),
        "losses": o.get("losses", 0),
        "avgDurationS": (round(sum(o["durations"]) / len(o["durations"]))
                         if o.get("durations") else None),
        "topLancers": sorted(o.get("lancers", {}).items(),
                             key=lambda kv: kv[1], reverse=True)[:3],
    }
    eff["_online"] = (entry or {}).get("online", []) if entry else []
    return eff


_STOP = set("the a an and or of to in on for with is are be can you your i my how "
            "what when where do does this that it as at by from".split())


def search_facts(query, limit=8):
    """RAG retrieval: return online facts most relevant to `query` (keyword overlap),
    each with its mode + source + trust. Used to ground the local LLM in FragPunk."""
    data = load()
    q = set(w for w in re.findall(r"[a-z0-9]+", (query or "").lower())
            if len(w) > 2 and w not in _STOP)
    scored = []
    trust_rank = {"official": 3, "wiki": 2, "creator": 1}
    for mk, ent in data["modes"].items():
        mk_words = set(mk.split("_"))
        for f in ent.get("online", []):
            fl = f.get("fact", "").lower()
            fw = set(re.findall(r"[a-z0-9]+", fl))
            overlap = len(q & fw) + (2 if (q & mk_words) else 0)
            if overlap:
                scored.append((overlap + 0.1 * trust_rank.get(f.get("trust"), 0),
                               {"mode": mk, "fact": f.get("fact"),
                                "source": f.get("source"), "trust": f.get("trust")}))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [f for _, f in scored[:limit]]


def summary():
    """Compact snapshot for /api/learning + the Coach UI."""
    data = load()
    modes = {}
    for key, entry in data["modes"].items():
        o = entry.get("observed", {})
        decided = o.get("wins", 0) + o.get("losses", 0)
        modes[key] = {
            "matches": o.get("matches", 0),
            "winRate": (round(100.0 * o.get("wins", 0) / decided) if decided else None),
            "onlineFacts": len(entry.get("online", [])),
            "events": {k: o.get(k, 0) for k in
                       ("respawn_seen", "revive_seen", "down_seen", "kill_seen", "death_seen")},
        }
    return {"modes": modes, "updated": data.get("updated", 0),
            "totalMatches": sum(m["matches"] for m in modes.values())}
