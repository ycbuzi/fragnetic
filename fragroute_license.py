"""FRAGROUTE licensing + entitlements -- Ed25519 signed license keys.

A license key is verified ENTIRELY OFFLINE using the embedded public key below
(no account, no internet). The matching PRIVATE key never ships -- it lives only
with the owner (keys/fragroute_ed25519_private.pem, gitignored) and signs keys via
mint_license.py. So the app can verify a key but can never forge one: a customer
cannot self-promote to a paid tier.

One key format covers all three sale models the owner chose:
  * OFFLINE  -- signature alone proves the tier (always works, no server).
  * SUBSCRIPTION -- the key carries an expiry; after it passes the tier drops to free.
  * ONLINE ACTIVATION -- if ONLINE_ENDPOINT is set, the key id + machine id are
    re-checked best-effort so the owner can revoke a leaked key / cap seats. The
    app stays usable offline (revocation only applies once a check actually says so).

Entitlement = the HIGHEST of: a valid license tier, a cloud-account tier (set by
fragroute_auth after a cloud login), and the free-trial tier (Pro for the first
TRIAL_DAYS). admin tier = unrestricted -- the owner's key.
"""
import base64
import json
import threading
import time
import uuid
import hashlib
from pathlib import Path

APP_LICENSE_BUILD = "lic-4"

# ---- verify-only public key (safe to ship; cannot sign with it) -------------
_PUBKEY_B64 = "kDUD32/uTxedly/hvXB6tIQLrla3bo/HTznhhO9Glqs="

TIERS = {"free": 0, "trial": 1, "pro": 2, "admin": 3}
TIER_LABEL = {"free": "Free", "trial": "Pro Trial", "pro": "Pro", "admin": "Admin (owner)"}

# capability -> minimum tier. Anything NOT listed here is free for everyone.
# NOTE: 'label'/'train' are ADMIN (owner) only -- those build/train the detector,
# which is the developer's job. Consumers just RUN the model that ships in updates,
# so they never see the labeling or training tools.
FEATURES = {
    # PREVIEW tier: unlocked during the 14-day trial AND for paid Pro (locked for Free).
    "coach": "trial", "detector": "trial", "reports": "trial", "live": "trial",
    # PREMIUM: PAID Pro only -- stays locked even during the trial (the reason to buy).
    "imagegen": "pro", "video": "pro", "footage": "pro",
    # Owner-only build/train tooling.
    "label": "admin", "train": "admin", "admin_tools": "admin",
}
# always-on free core (shown so the UI can label them)
FREE_FEATURES = ["queue", "vpn", "overlay", "locker", "stats", "setup", "cards"]

# The OWNER's machine gets admin automatically -- no key needed -- but ONLY on this
# exact PC. machine_id() is derived from this box's MAC + hostname; a customer cannot
# reproduce it, so shipping this constant in the build is safe (it unlocks nothing on
# their hardware). This is how "admin is locked to my PC only" is enforced.
OWNER_MACHINE_IDS = {"a4b4d266c63e7992"}

TRIAL_DAYS = 14
KEY_PREFIX = "FRG1"

BASE_DIR = None             # set by engine (folder holding license.json + trial marker)
ONLINE_ENDPOINT = None      # optional: "https://your.host/activate" for revoke/seats
_LOCK = threading.Lock()
_ACCOUNT_TIER = {"tier": None, "name": None}   # set by fragroute_auth on cloud login
_ONLINE_CACHE = {}          # license_id -> {"ok":bool, "revoked":bool, "ts":float}
_ONLINE_TTL = 6 * 3600

# --- Lemon Squeezy built-in license keys (no self-hosted server needed) -------
# LS generates + emails a key on every purchase; the app activates/validates it
# against LS's public License API using ONLY the key itself (no store secret ships
# in the app). We cache the result so it works OFFLINE after the first activation,
# and re-validate in the background so a cancelled subscription eventually drops to
# free. Both these LS keys AND the owner's Ed25519 (FRG1) keys work side by side.
LS_API = "https://api.lemonsqueezy.com/v1/licenses"
# LS variant id -> tier. 1863450 = "Fragnetic Pro" (confirmed from a real test-mode
# key). Since Pro is the ONLY paid product, LS_DEFAULT_TIER already grants Pro to any
# valid LS key -- this map is just belt-and-suspenders / future-proofing for if a
# non-Pro variant is ever added.
LS_VARIANT_TIERS = {"1863450": "pro"}
LS_DEFAULT_TIER = "pro"
LS_REVALIDATE_TTL = 3 * 86400      # re-check a key online ~every 3 days (best effort)
_LS_REVAL = {"on": False}


# ---------------------------------------------------------------- helpers ----
def _base():
    if BASE_DIR:
        return Path(BASE_DIR)
    import sys
    return (Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent)


def _lic_path():
    return _base() / "fragroute_license.json"


def _trial_path():
    return _base() / ".fragroute_trial"


def _pub():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    return Ed25519PublicKey.from_public_bytes(base64.b64decode(_PUBKEY_B64))


def machine_id():
    """Stable-ish per-machine id for seat tracking (best effort, not a secret)."""
    raw = "%s|%s" % (uuid.getnode(), __import__("platform").node())
    return hashlib.sha256(raw.encode("utf-8", "ignore")).hexdigest()[:16]


def _b64u_dec(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def rank(tier):
    return TIERS.get(tier, 0)


# ---------------------------------------------------------- key verification --
def verify_key(key):
    """Verify a license key string OFFLINE. Returns a dict describing it (with
    'valid'/'expired'), or {'valid': False, 'error': ...} if the signature is bad."""
    from cryptography.exceptions import InvalidSignature
    try:
        key = (key or "").strip().replace("\n", "")
        parts = key.split(".")
        if len(parts) != 3 or parts[0] != KEY_PREFIX:
            return {"valid": False, "error": "not a FRAGROUTE license key"}
        payload_b = _b64u_dec(parts[1])
        sig = _b64u_dec(parts[2])
        _pub().verify(sig, payload_b)            # raises if forged/tampered
        p = json.loads(payload_b.decode("utf-8"))
        exp = int(p.get("x", 0) or 0)
        expired = bool(exp) and time.time() > exp
        tier = p.get("t", "free")
        if tier not in TIERS:
            tier = "free"
        return {"valid": True, "tier": tier, "name": p.get("n", ""),
                "exp": exp, "expired": expired, "id": p.get("i", ""),
                "seats": int(p.get("s", 1) or 1), "created": int(p.get("c", 0) or 0)}
    except InvalidSignature:
        return {"valid": False, "error": "signature invalid (forged or corrupted key)"}
    except Exception as e:
        return {"valid": False, "error": "bad key: %s" % str(e)[:60]}


def _looks_like_ls_key(key):
    """A Lemon Squeezy key is a UUID-style string (hex groups joined by hyphens),
    not our dotted FRG1.<payload>.<sig> format. Used to route to the right path."""
    k = (key or "").strip()
    return bool(k) and not k.startswith(KEY_PREFIX) and "." not in k and k.count("-") >= 3


def _ls_call(action, key, instance_name=None, instance_id=None, timeout=10):
    """Call the LS License API (activate|validate|deactivate). Key-only auth --
    no store secret. Returns the parsed JSON dict (or raises on network error)."""
    import urllib.request
    import urllib.parse
    data = {"license_key": key}
    if instance_name:
        data["instance_name"] = instance_name
    if instance_id:
        data["instance_id"] = instance_id
    body = urllib.parse.urlencode(data).encode("utf-8")
    req = urllib.request.Request(LS_API + "/" + action, data=body,
                                 headers={"Accept": "application/json",
                                          "Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "ignore"))
    except urllib.error.HTTPError as e:      # LS returns 400/404 with a JSON error body
        try:
            return json.loads(e.read().decode("utf-8", "ignore"))
        except Exception:
            return {"error": "HTTP %s" % e.code}


def _tier_for_variant(variant_id):
    return LS_VARIANT_TIERS.get(str(variant_id or ""), LS_DEFAULT_TIER)


def _set_ls_license(key):
    """Activate a Lemon Squeezy key and cache it locally. Reuses the existing
    activation if this exact key was already activated on this machine (so
    re-pasting doesn't burn one of the buyer's limited activations)."""
    key = key.strip()
    prev = _saved_record()
    if prev and prev.get("type") == "ls" and prev.get("key") == key and prev.get("instance"):
        try:
            res = _ls_call("validate", key, instance_id=prev["instance"])
            if res.get("valid"):
                prev["validated"] = time.time()
                prev["revoked"] = False
                _lic_path().write_text(json.dumps(prev), encoding="utf-8")
                return {"valid": True, "tier": prev.get("tier", LS_DEFAULT_TIER),
                        "name": prev.get("name", ""), "ls": True}
        except Exception:
            pass                             # fall through to a fresh activation
    try:
        res = _ls_call("activate", key, instance_name=machine_id())
    except Exception as e:
        return {"valid": False, "error": "couldn't reach the license server "
                "(check your internet and try again): %s" % str(e)[:50]}
    if not res.get("activated"):
        err = res.get("error") or "this key wasn't accepted"
        return {"valid": False, "error": err}
    meta = res.get("meta") or {}
    lk = res.get("license_key") or {}
    inst = res.get("instance") or {}
    tier = _tier_for_variant(meta.get("variant_id"))
    rec = {"type": "ls", "key": key, "tier": tier,
           "variant": str(meta.get("variant_id") or ""),
           "instance": inst.get("id"), "name": meta.get("customer_name") or "",
           "email": meta.get("customer_email") or "", "status": lk.get("status"),
           "validated": time.time(), "revoked": False}
    try:
        _lic_path().write_text(json.dumps(rec), encoding="utf-8")
    except Exception as e:
        return {"valid": False, "error": "could not save: %s" % e}
    left = (lk.get("activation_limit") or 0) - (lk.get("activation_usage") or 0)
    return {"valid": True, "tier": tier, "name": rec["name"], "ls": True,
            "activationsLeft": max(0, left)}


def _ls_maybe_revalidate(rec):
    """Kick a BACKGROUND re-validation if the cached LS record is stale, so a
    cancelled/expired subscription eventually drops to free without ever blocking
    the app or breaking offline use."""
    if time.time() - rec.get("validated", 0) < LS_REVALIDATE_TTL:
        return
    if _LS_REVAL["on"]:
        return
    _LS_REVAL["on"] = True

    def _go():
        try:
            res = _ls_call("validate", rec.get("key"), instance_id=rec.get("instance"))
            lk = res.get("license_key") or {}
            status = lk.get("status")
            rec["validated"] = time.time()
            rec["status"] = status
            # Per LS docs, the authoritative signal is `valid`. A cancelled/expired
            # subscription -> status 'expired'; a manual kill -> 'disabled'. 'inactive'
            # just means "valid key, no activations" and must NOT drop paid users.
            rec["revoked"] = (not res.get("valid")) or status in ("disabled", "expired")
            if not rec["revoked"]:
                meta = res.get("meta") or {}
                if meta.get("variant_id"):
                    rec["tier"] = _tier_for_variant(meta.get("variant_id"))
            _lic_path().write_text(json.dumps(rec), encoding="utf-8")
        except Exception:
            pass                             # offline / transient -> keep the cache
        finally:
            _LS_REVAL["on"] = False
    threading.Thread(target=_go, daemon=True).start()


def _ls_entitlement(rec):
    """verify_key()-shaped result from a cached LS record (+ background re-check)."""
    _ls_maybe_revalidate(rec)
    if rec.get("revoked"):
        return {"valid": True, "tier": "free", "name": rec.get("name", ""),
                "expired": True, "ls": True, "id": rec.get("instance", "")}
    return {"valid": True, "tier": rec.get("tier", LS_DEFAULT_TIER),
            "name": rec.get("name", ""), "expired": False, "ls": True,
            "id": rec.get("instance", "")}


def set_license(key):
    """Validate + store a license key. Routes Lemon Squeezy keys to online
    activation and the owner's FRG1 keys to offline signature verification."""
    key = (key or "").strip()
    if _looks_like_ls_key(key):
        return _set_ls_license(key)
    info = verify_key(key)
    if not info.get("valid"):
        return info
    try:
        _lic_path().write_text(json.dumps({"key": key}), encoding="utf-8")
    except Exception as e:
        return {"valid": False, "error": "could not save: %s" % e}
    _ONLINE_CACHE.pop(info.get("id"), None)
    return info


def clear_license():
    # if it's an LS key, deactivate this machine's instance so the buyer gets that
    # activation back (best effort -- never blocks removal).
    rec = _saved_record()
    if rec and rec.get("type") == "ls" and rec.get("instance"):
        try:
            _ls_call("deactivate", rec.get("key"), instance_id=rec.get("instance"), timeout=6)
        except Exception:
            pass
    try:
        _lic_path().unlink()
    except Exception:
        pass
    return {"ok": True}


def _saved_record():
    try:
        return json.loads(_lic_path().read_text(encoding="utf-8"))
    except Exception:
        return None


def _saved_license():
    rec = _saved_record()
    if not rec:
        return None
    if rec.get("type") == "ls":              # Lemon Squeezy key -> cached entitlement
        return _ls_entitlement(rec)
    key = rec.get("key")                     # owner's FRG1 Ed25519 key -> offline verify
    return verify_key(key) if key else None


# ------------------------------------------------------------------- trial ----
def _trial_start():
    """Epoch the trial began (created on first call). Lightly obfuscated; a
    determined local user can reset it -- standard for an offline trial."""
    p = _trial_path()
    try:
        if p.exists():
            d = json.loads(_b64u_dec(p.read_text(encoding="utf-8").strip()).decode())
            # integrity tag so a casual edit of the epoch is ignored
            if d.get("h") == hashlib.sha256(("frg%s" % d.get("s")).encode()).hexdigest()[:12]:
                return int(d.get("s", 0))
    except Exception:
        pass
    s = int(time.time())
    try:
        blob = {"s": s, "h": hashlib.sha256(("frg%s" % s).encode()).hexdigest()[:12]}
        p.write_text(base64.urlsafe_b64encode(json.dumps(blob).encode()).decode(), encoding="utf-8")
        try:
            import subprocess
            subprocess.run(["attrib", "+h", str(p)],           # hide it on Windows
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=5, check=False)
        except Exception:
            pass
    except Exception:
        pass
    return s


def trial_days_left():
    used = (time.time() - _trial_start()) / 86400.0
    return max(0, int(round(TRIAL_DAYS - used)))


# --------------------------------------------------------- online re-check ----
def _online_check(lic):
    """Best-effort revocation / seat check. Never blocks usage on failure."""
    if not ONLINE_ENDPOINT or not lic or not lic.get("id"):
        return None
    lid = lic["id"]
    c = _ONLINE_CACHE.get(lid)
    if c and (time.time() - c["ts"]) < _ONLINE_TTL:
        return c
    try:
        import urllib.request
        import urllib.parse
        url = ONLINE_ENDPOINT + "?" + urllib.parse.urlencode(
            {"id": lid, "m": machine_id(), "v": APP_LICENSE_BUILD})
        req = urllib.request.Request(url, headers={"User-Agent": "FRAGROUTE-lic"})
        with urllib.request.urlopen(req, timeout=6) as r:
            d = json.loads(r.read().decode("utf-8"))
        res = {"ok": True, "revoked": bool(d.get("revoked")),
               "seatsLeft": d.get("seatsLeft"), "ts": time.time()}
    except Exception as e:
        res = {"ok": False, "error": str(e)[:60], "ts": time.time()}
    _ONLINE_CACHE[lid] = res
    return res


# --------------------------------------------------- account-tier (cloud) ----
def set_account_tier(tier, name=None):
    """fragroute_auth calls this after a cloud login so a cloud account can ALSO
    grant a tier (the 'both' model). Cleared on logout with tier=None."""
    _ACCOUNT_TIER["tier"] = tier if tier in TIERS else None
    _ACCOUNT_TIER["name"] = name


# ----------------------------------------------------------- entitlement ----
def entitlement():
    """Resolve the effective tier + per-feature unlock. Highest of license,
    cloud-account, and trial."""
    lic = _saved_license()
    sources = []
    best = "free"
    holder = None
    exp = 0
    revoked = False

    if lic and lic.get("valid") and not lic.get("expired"):
        oc = _online_check(lic)
        if oc and oc.get("ok") and oc.get("revoked"):
            revoked = True                       # server says this key is dead
        else:
            if rank(lic["tier"]) > rank(best):
                best = lic["tier"]
            holder = lic.get("name") or holder
            exp = lic.get("exp") or exp
            sources.append("license")

    if _ACCOUNT_TIER["tier"] and rank(_ACCOUNT_TIER["tier"]) > rank(best):
        best = _ACCOUNT_TIER["tier"]
        holder = _ACCOUNT_TIER["name"] or holder
        sources.append("account")

    tdl = trial_days_left()
    # The trial grants the PREVIEW tier (coach/live/reports/detector), NOT full Pro --
    # premium features (image gen, video editor, footage recorder) stay locked so the
    # trial is a genuine preview, not a free Pro giveaway.
    trial_active = tdl > 0 and rank(best) < rank("trial")
    if trial_active:
        best = "trial"
        sources.append("trial")

    # Owner's own PC -> admin, no key required (safe: tied to this machine's id).
    if machine_id() in OWNER_MACHINE_IDS and rank(best) < rank("admin"):
        best = "admin"
        holder = holder or "Owner (this PC)"
        sources.append("owner-machine")

    feats = {}
    for feat, need in FEATURES.items():
        feats[feat] = rank(best) >= rank(need)
    for feat in FREE_FEATURES:
        feats[feat] = True

    return {
        "tier": best, "tierLabel": TIER_LABEL.get(best, best),
        "sources": sources, "holder": holder, "exp": exp,
        "trialActive": trial_active, "trialDaysLeft": tdl, "trialTotal": TRIAL_DAYS,
        "revoked": revoked, "licenseExpired": bool(lic and lic.get("expired")),
        "features": feats, "machineId": machine_id(),
        "online": bool(ONLINE_ENDPOINT),
    }


def is_enabled(feature):
    """The server-side gate. Engine endpoints call this BEFORE doing paid work
    (UI hiding alone is bypassable)."""
    if feature in FREE_FEATURES or feature not in FEATURES:
        return True
    return entitlement()["features"].get(feature, False)


def status():
    e = entitlement()
    lic = _saved_license()
    e["hasLicense"] = bool(lic and lic.get("valid"))
    e["licenseTier"] = lic.get("tier") if lic else None
    e["build"] = APP_LICENSE_BUILD
    return e
