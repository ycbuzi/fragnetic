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

APP_LICENSE_BUILD = "lic-1"

# ---- verify-only public key (safe to ship; cannot sign with it) -------------
_PUBKEY_B64 = "kDUD32/uTxedly/hvXB6tIQLrla3bo/HTznhhO9Glqs="

TIERS = {"free": 0, "pro": 1, "admin": 2}
TIER_LABEL = {"free": "Free", "pro": "Pro", "admin": "Admin (owner)"}

# capability -> minimum tier. Anything NOT listed here is free for everyone.
# NOTE: 'label'/'train' are ADMIN (owner) only -- those build/train the detector,
# which is the developer's job. Consumers just RUN the model that ships in updates,
# so they never see the labeling or training tools.
FEATURES = {
    "coach": "pro", "imagegen": "pro", "video": "pro", "detector": "pro",
    "reports": "pro", "live": "pro",
    "label": "admin", "train": "admin", "admin_tools": "admin",
}
# always-on free core (shown so the UI can label them)
FREE_FEATURES = ["queue", "vpn", "overlay", "locker", "stats", "setup", "cards"]

TRIAL_DAYS = 14
KEY_PREFIX = "FRG1"

BASE_DIR = None             # set by engine (folder holding license.json + trial marker)
ONLINE_ENDPOINT = None      # optional: "https://your.host/activate" for revoke/seats
_LOCK = threading.Lock()
_ACCOUNT_TIER = {"tier": None, "name": None}   # set by fragroute_auth on cloud login
_ONLINE_CACHE = {}          # license_id -> {"ok":bool, "revoked":bool, "ts":float}
_ONLINE_TTL = 6 * 3600


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


def set_license(key):
    """Validate + store a license key. Returns the verify_key() result."""
    info = verify_key(key)
    if not info.get("valid"):
        return info
    try:
        _lic_path().write_text(json.dumps({"key": key.strip()}), encoding="utf-8")
    except Exception as e:
        return {"valid": False, "error": "could not save: %s" % e}
    _ONLINE_CACHE.pop(info.get("id"), None)
    return info


def clear_license():
    try:
        _lic_path().unlink()
    except Exception:
        pass
    return {"ok": True}


def _saved_license():
    try:
        key = json.loads(_lic_path().read_text(encoding="utf-8")).get("key")
        return verify_key(key) if key else None
    except Exception:
        return None


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
            import os
            os.system('attrib +h "%s" >nul 2>&1' % p)   # hide it on Windows
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
    trial_active = tdl > 0 and rank(best) < rank("pro")
    if trial_active:
        best = "pro"
        sources.append("trial")

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
