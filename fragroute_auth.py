"""FRAGROUTE accounts -- local-first, optional cloud.

LOCAL: usernames + PBKDF2-HMAC-SHA256 password hashes in fragroute_accounts.json
(never plaintext). Works fully offline. The login screen gates the app UI.

CLOUD (optional, "both" model): if CLOUD_ENDPOINT is set the same register/login
also talk to the owner's server, which can assign a tier and let the owner manage
customers centrally. If the server is unreachable the app falls back to the local
account, so it never blocks usage offline.

SECURITY NOTE: the login is a PROFILE gate, not the paid-feature gate. Entitlement
comes from the signed license key (fragroute_license) -- an account can CARRY a key
(so the owner's account holds the admin key and logging in unlocks everything), but
the username/role alone never grants a tier. That's why a customer can't create an
"admin" account and get unrestricted access.
"""
import base64
import hashlib
import json
import os
import threading
import time
from pathlib import Path

import fragroute_license as L

APP_AUTH_BUILD = "auth-1"

BASE_DIR = None
CLOUD_ENDPOINT = None          # e.g. "https://your.host/api" (optional)
PBKDF_ITERS = 200_000
MIN_PW = 8

_LOCK = threading.Lock()
_SESSION = {"user": None}      # in-memory current login


def _base():
    if BASE_DIR:
        return Path(BASE_DIR)
    import sys
    return (Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent)


def _path():
    return _base() / "fragroute_accounts.json"


def _load():
    try:
        return json.loads(_path().read_text(encoding="utf-8"))
    except Exception:
        return {"users": {}}


def _save(d):
    tmp = str(_path()) + ".tmp"
    Path(tmp).write_text(json.dumps(d, indent=2), encoding="utf-8")
    os.replace(tmp, _path())


def _hash(password, salt_b):
    return base64.b64encode(
        hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt_b, PBKDF_ITERS)
    ).decode()


def _norm(u):
    return (u or "").strip().lower()


def _gen_recovery():
    """A one-time recovery code shown at signup (offline password reset). Avoids
    look-alike characters so it's easy to write down."""
    import secrets
    alpha = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "-".join("".join(secrets.choice(alpha) for _ in range(4)) for _ in range(4))


def _public(rec):
    """Account fields safe to expose to the UI (never the hash/salt)."""
    return {"username": rec.get("username"), "email": rec.get("email", ""),
            "role": rec.get("role", "user"), "created": rec.get("created"),
            "hasLicense": bool(rec.get("license")), "cloud": rec.get("cloud", False)}


# --------------------------------------------------------------- queries ----
def has_any_account():
    return bool(_load().get("users"))


def current():
    u = _SESSION["user"]
    if not u:
        return {"loggedIn": False}
    rec = _load().get("users", {}).get(_norm(u))
    if not rec:
        return {"loggedIn": False}
    out = {"loggedIn": True}
    out.update(_public(rec))
    return out


# --------------------------------------------------------- apply / session ----
def _apply_account_entitlement(rec):
    """Feed this account's tier into the license layer (cloud tier or carried key)."""
    if rec.get("cloud") and rec.get("tier"):
        L.set_account_tier(rec["tier"], rec.get("username"))
    elif rec.get("license"):
        info = L.verify_key(rec["license"])
        if info.get("valid") and not info.get("expired"):
            L.set_account_tier(info["tier"], rec.get("username"))
    else:
        L.set_account_tier(None)


def _start_session(rec):
    _SESSION["user"] = rec.get("username")
    _apply_account_entitlement(rec)


def logout():
    _SESSION["user"] = None
    L.set_account_tier(None)
    return {"ok": True}


# ----------------------------------------------------------- register/login --
def register(username, password, email="", license_key=""):
    username = (username or "").strip()
    if len(username) < 3:
        return {"ok": False, "error": "username must be at least 3 characters"}
    if len(password or "") < MIN_PW:
        return {"ok": False, "error": "password must be at least %d characters" % MIN_PW}
    with _LOCK:
        d = _load()
        users = d.setdefault("users", {})
        if _norm(username) in users:
            return {"ok": False, "error": "that username is taken"}
        salt = os.urandom(16)
        rec = {"username": username, "email": email.strip(),
               "salt": base64.b64encode(salt).decode(), "hash": _hash(password, salt),
               "role": "owner" if not users else "user",
               "created": int(time.time()), "cloud": False}
        # one-time recovery code (stored hashed; returned in plaintext just this once)
        recovery = _gen_recovery()
        rsalt = os.urandom(16)
        rec["rsalt"] = base64.b64encode(rsalt).decode()
        rec["recovery"] = _hash(recovery, rsalt)
        if license_key:
            info = L.verify_key(license_key)
            if not info.get("valid"):
                return {"ok": False, "error": "license: %s" % info.get("error", "invalid")}
            rec["license"] = license_key.strip()
        # optional cloud mirror (best effort; never blocks local create)
        cloud = _cloud_call("register", {"username": username, "email": email,
                                         "password": password}) if CLOUD_ENDPOINT else None
        if cloud and cloud.get("ok"):
            rec["cloud"] = True
            if cloud.get("tier"):
                rec["tier"] = cloud["tier"]
        users[_norm(username)] = rec
        _save(d)
    _start_session(rec)
    return {"ok": True, "user": _public(rec), "recovery": recovery}


def login(username, password):
    rec = _load().get("users", {}).get(_norm(username))
    if not rec:
        # maybe a cloud-only account on a fresh machine
        cloud = _cloud_call("login", {"username": username, "password": password}) if CLOUD_ENDPOINT else None
        if cloud and cloud.get("ok"):
            salt = os.urandom(16)
            rec = {"username": username, "email": cloud.get("email", ""),
                   "salt": base64.b64encode(salt).decode(), "hash": _hash(password, salt),
                   "role": "user", "created": int(time.time()),
                   "cloud": True, "tier": cloud.get("tier")}
            with _LOCK:
                d = _load(); d.setdefault("users", {})[_norm(username)] = rec; _save(d)
            _start_session(rec)
            return {"ok": True, "user": _public(rec)}
        return {"ok": False, "error": "no such account"}
    if _hash(password, base64.b64decode(rec["salt"])) != rec.get("hash"):
        return {"ok": False, "error": "wrong password"}
    # refresh tier from cloud if applicable
    if rec.get("cloud") and CLOUD_ENDPOINT:
        cloud = _cloud_call("login", {"username": username, "password": password})
        if cloud and cloud.get("ok") and cloud.get("tier"):
            rec["tier"] = cloud["tier"]
            with _LOCK:
                d = _load(); d["users"][_norm(username)] = rec; _save(d)
    _start_session(rec)
    return {"ok": True, "user": _public(rec)}


def change_password(old, new):
    u = _SESSION["user"]
    if not u:
        return {"ok": False, "error": "not logged in"}
    with _LOCK:
        d = _load(); rec = d.get("users", {}).get(_norm(u))
        if not rec or _hash(old, base64.b64decode(rec["salt"])) != rec.get("hash"):
            return {"ok": False, "error": "current password is wrong"}
        if len(new or "") < MIN_PW:
            return {"ok": False, "error": "new password must be at least %d characters" % MIN_PW}
        salt = os.urandom(16)
        rec["salt"] = base64.b64encode(salt).decode(); rec["hash"] = _hash(new, salt)
        _save(d)
    return {"ok": True}


def reset_password(username, code, new_password):
    """Reset a forgotten password using the recovery code shown at signup. The
    used code is rotated so it can't be replayed."""
    if len(new_password or "") < MIN_PW:
        return {"ok": False, "error": "new password must be at least %d characters" % MIN_PW}
    with _LOCK:
        d = _load()
        rec = d.get("users", {}).get(_norm(username))
        if not rec or not rec.get("recovery") or not rec.get("rsalt"):
            return {"ok": False, "error": "no recovery code on file for that account"}
        if _hash((code or "").strip().upper(), base64.b64decode(rec["rsalt"])) != rec["recovery"]:
            return {"ok": False, "error": "recovery code is incorrect"}
        salt = os.urandom(16)
        rec["salt"] = base64.b64encode(salt).decode()
        rec["hash"] = _hash(new_password, salt)
        new_code = _gen_recovery()                      # rotate so the used code dies
        rsalt = os.urandom(16)
        rec["rsalt"] = base64.b64encode(rsalt).decode()
        rec["recovery"] = _hash(new_code, rsalt)
        _save(d)
    return {"ok": True, "recovery": new_code}


def attach_license(license_key):
    """Bind a license key to the current account (so login re-applies it)."""
    u = _SESSION["user"]
    if not u:
        return {"ok": False, "error": "not logged in"}
    info = L.verify_key(license_key)
    if not info.get("valid"):
        return {"ok": False, "error": info.get("error", "invalid key")}
    with _LOCK:
        d = _load(); rec = d.get("users", {}).get(_norm(u))
        rec["license"] = license_key.strip(); _save(d)
    _apply_account_entitlement(rec)
    return {"ok": True, "tier": info["tier"]}


# ------------------------------------------------------------- cloud client --
def _cloud_call(action, body, timeout=6):
    """POST to the optional cloud endpoint. Returns parsed JSON or None on any
    failure (offline-tolerant)."""
    if not CLOUD_ENDPOINT:
        return None
    try:
        import urllib.request
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(CLOUD_ENDPOINT.rstrip("/") + "/" + action,
                                     data=data, headers={"Content-Type": "application/json",
                                                         "User-Agent": "FRAGROUTE-auth"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None


def status():
    return {"build": APP_AUTH_BUILD, "hasAccount": has_any_account(),
            "cloud": bool(CLOUD_ENDPOINT), "session": current()}
