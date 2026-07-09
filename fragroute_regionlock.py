"""FRAGROUTE Direct Region Lock -- switch FragPunk's matchmaking region WITHOUT a
VPN by firewall-blocking the match-server IP ranges of the regions you DON'T want.
The matchmaker then can only reach the region you left open, and you connect
DIRECTLY (no VPN exit-hop -> often lower ping than a VPN to the same region).

We are already `--uac-admin`, so Windows Firewall rules (netsh advfirewall) are
available. The WireGuard VPN stays as the fallback for regions that won't fall back
cleanly.

## What we block (learned from live capture 2026-07-01)
A FragPunk match uses:
  * UDP :7800            -> the real-time GAMEPLAY traffic
  * TCP :9020 / :9081    -> the match/session server
while the things we must NEVER break live on:
  * TCP :11000           -> the persistent LOBBY
  * TCP :18110           -> the home BACKEND (account/social)
  * TCP :443 (/ :80)     -> web/API/telemetry
The lobby IP can share a region prefix with that region's match servers (e.g. the
us-east lobby 8.221.58.250 sits in 8.221.x alongside us-east match servers), so we
CANNOT protect the lobby by excluding an IP -- we protect it by PORT. Hence:
  * block a region's CIDRs on **UDP: all ports** (kills gameplay 7800), and
  * block a region's CIDRs on **TCP: every port EXCEPT the whitelist** (kills the
    session 9020/9081 while the lobby :11000 / backend :18110 / web :443 survive).

## Safety
- Applies NOTHING on its own -- the engine/UI must call apply() after explicit user OK.
- Every rule is named with RULE_PREFIX and tracked in a state file, so clear() is
  total and a crash-leftover is auto-removed on next launch.
- Outbound-only block (the game dials out; blocking the SYN is enough and is the
  least-invasive, fully-reversible option).

Pure stdlib (subprocess/netsh). The engine passes region->CIDR data; this module is
otherwise data-agnostic.
"""
import json
import os
import subprocess
import threading
from pathlib import Path

APP_REGIONLOCK_BUILD = "rlock-1"

RULE_PREFIX = "FragneticRegionLock"
# TCP ports that must ALWAYS stay open (web / anti-cheat / lobby / backend). UDP has
# no such needs -- these services are all TCP -- so UDP for a blocked region is fully
# blocked. :7777 is the NetEase anti-cheat (NEAC) control port, observed live in the
# player's HOME-region cloud range; never block it or the anti-cheat drops mid-match
# (kick / account flag). The engine ALSO carves the anti-cheat's live IP out of the
# block map by /32, so this port entry is the second layer for the "lock applied
# before the game launched" case (no live connection to read yet).
WHITELIST_TCP_PORTS = [443, 7777, 11000, 18110]

STATE_DIR = None                     # set by engine (folder that holds the exe/state)
_LOCK = threading.Lock()
_NOWIN = {"creationflags": 0x08000000} if os.name == "nt" else {}


# --------------------------------------------------------------------------- #
#  environment
# --------------------------------------------------------------------------- #
def _state_path():
    base = Path(STATE_DIR) if STATE_DIR else Path(__file__).parent
    return base / "fragroute_regionlock.json"


def available():
    """netsh present (Windows). Region lock needs the Windows firewall CLI."""
    if os.name != "nt":
        return False
    try:
        p = subprocess.run(["netsh", "advfirewall", "show", "allprofiles", "state"],
                           capture_output=True, text=True, errors="replace", timeout=8, **_NOWIN)
        return p.returncode == 0
    except Exception:
        return False


def is_admin():
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


# --------------------------------------------------------------------------- #
#  state (which rules we've applied)
# --------------------------------------------------------------------------- #
def _load_state():
    try:
        return json.loads(_state_path().read_text(encoding="utf-8"))
    except Exception:
        return {"active": False, "blocked": [], "rules": [], "target": None}


def _save_state(st):
    try:
        _state_path().write_text(json.dumps(st, indent=2), encoding="utf-8")
    except Exception:
        pass


# --------------------------------------------------------------------------- #
#  rule building
# --------------------------------------------------------------------------- #
def _tcp_block_port_ranges():
    """The complement of WHITELIST_TCP_PORTS across 1-65535, as a netsh remoteport
    list (e.g. '1-442,444-10999,11001-18109,18111-65535'). Blocking these TCP ports
    to a region kills its match/session servers while the whitelisted lobby/backend/
    web ports stay reachable."""
    wl = sorted(set(int(p) for p in WHITELIST_TCP_PORTS))
    ranges = []
    start = 1
    for p in wl:
        if p > start:
            ranges.append((start, p - 1))
        start = p + 1
    if start <= 65535:
        ranges.append((start, 65535))
    return ",".join(("%d-%d" % (a, b)) if a != b else ("%d" % a) for a, b in ranges)


def _rule_name(region_id, proto):
    safe = "".join(c for c in str(region_id) if c.isalnum() or c in "-_") or "region"
    return "%s_%s_%s" % (RULE_PREFIX, safe, proto)


def _clean_cidrs(cidrs):
    """Keep only well-formed IPv4 addresses / CIDRs; dedupe; cap length so a netsh
    command never blows past its limits."""
    out = []
    seen = set()
    for c in (cidrs or []):
        c = str(c).strip()
        if not c or c in seen:
            continue
        head = c.split("/")[0]
        parts = head.split(".")
        if len(parts) != 4:
            continue
        try:
            if not all(0 <= int(x) <= 255 for x in parts):
                continue
        except Exception:
            continue
        seen.add(c)
        out.append(c)
    return out[:400]                     # netsh handles long lists, but stay sane


def plan(block_map):
    """Return the netsh commands we WOULD run for a {region_id: [cidr,...]} map,
    without touching the firewall. For the UI preview + the 'nothing applied without
    OK' guarantee. Returns a list of {region, proto, cidrs, cmd}."""
    cmds = []
    tcp_ports = _tcp_block_port_ranges()
    for rid, cidrs in (block_map or {}).items():
        cc = _clean_cidrs(cidrs)
        if not cc:
            continue
        iplist = ",".join(cc)
        # UDP: block ALL ports (gameplay 7800) for this region's servers
        cmds.append({
            "region": rid, "proto": "UDP", "cidrs": cc,
            "cmd": ["netsh", "advfirewall", "firewall", "add", "rule",
                    "name=" + _rule_name(rid, "udp"), "dir=out", "action=block",
                    "protocol=UDP", "remoteip=" + iplist, "enable=yes"]})
        # TCP: block every port EXCEPT the lobby/backend/web whitelist (session 9020/9081)
        cmds.append({
            "region": rid, "proto": "TCP", "cidrs": cc,
            "cmd": ["netsh", "advfirewall", "firewall", "add", "rule",
                    "name=" + _rule_name(rid, "tcp"), "dir=out", "action=block",
                    "protocol=TCP", "remoteip=" + iplist,
                    "remoteport=" + tcp_ports, "enable=yes"]})
    return cmds


# --------------------------------------------------------------------------- #
#  apply / clear
# --------------------------------------------------------------------------- #
def _run(cmd):
    try:
        # errors="replace": netsh emits localized firewall text that isn't valid cp1252 (bytes
        # like 0x81/0x8f). WITHOUT this, the subprocess reader thread dies with UnicodeDecodeError,
        # the output is lost, and the firewall op looks failed/unverified. (Root cause of a
        # recurring diag-log crash every firewall reconcile.)
        p = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=20, **_NOWIN)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except Exception as e:
        return 1, str(e)


def apply(block_map, target_region=None):
    """Apply the firewall block for {region_id: [cidr,...]} (regions to BLOCK). Clears
    any prior lock first so it's always a clean set. Returns {ok, blocked, rules, ...}.
    CALLER MUST HAVE EXPLICIT USER CONSENT -- this changes the Windows firewall."""
    if not available():
        return {"ok": False, "message": "Windows Firewall (netsh) unavailable."}
    if not is_admin():
        return {"ok": False, "message": "Region lock needs admin (the app runs elevated -- restart it)."}
    with _LOCK:
        clear()                          # start from a known-clean state
        planned = plan(block_map)
        if not planned:
            return {"ok": False, "message": "No server ranges to block yet -- play a few "
                                            "matches so the map fills, or add ranges."}
        applied = []
        failed = []
        for item in planned:
            rc, log = _run(item["cmd"])
            if rc == 0:
                applied.append(_rule_name(item["region"], item["proto"].lower()))
            else:
                failed.append({"region": item["region"], "proto": item["proto"],
                               "err": log.strip()[:160]})
        blocked = sorted({i["region"] for i in planned})
        st = {"active": bool(applied), "blocked": blocked, "rules": applied,
              "target": target_region}
        _save_state(st)
        if failed and not applied:
            return {"ok": False, "message": "Failed to add firewall rules.",
                    "failed": failed}
        return {"ok": True, "active": True, "blocked": blocked, "target": target_region,
                "rules": applied, "failed": failed,
                "message": "Region lock ON -- blocked %d region(s)%s."
                           % (len(blocked), (" -> " + target_region) if target_region else "")}


def _sweep_prefix():
    """Delete EVERY firewall rule whose name starts with our prefix, regardless of the
    state file -- the authoritative safety net. apply() writes the state file AFTER it
    adds the netsh rules, so a hard-kill in between (or a lost/corrupted state file)
    leaves rules the state doesn't record; without this sweep those would silently keep
    blocking the user's connections with nothing to remove them. netsh has no wildcard
    delete, so use PowerShell's NetSecurity module (netsh 'name' == rule DisplayName).
    Best-effort; returns how many it removed (0 if none / PowerShell unavailable)."""
    if os.name != "nt":
        return 0
    try:
        ps = ("$r=Get-NetFirewallRule -DisplayName '%s*' -ErrorAction SilentlyContinue;"
              "if($r){$c=@($r).Count; $r | Remove-NetFirewallRule -ErrorAction SilentlyContinue; $c}"
              "else{0}" % RULE_PREFIX)
        out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                             capture_output=True, text=True, errors="replace",
                             timeout=25, **_NOWIN).stdout.strip()
        return int((out or "0").splitlines()[-1]) if out else 0
    except Exception:
        return 0


def clear():
    """Remove EVERY rule this module created (tracked names + a belt-and-suspenders
    sweep of the prefix). Safe to call anytime; total and idempotent. Returns
    {ok, removed}."""
    removed = 0
    st = _load_state()
    names = list(st.get("rules") or [])
    # also cover both proto suffixes for every region we recorded, in case state drifted
    for rid in (st.get("blocked") or []):
        for proto in ("udp", "tcp"):
            n = _rule_name(rid, proto)
            if n not in names:
                names.append(n)
    for name in names:
        rc, _ = _run(["netsh", "advfirewall", "firewall", "delete", "rule", "name=" + name])
        if rc == 0:
            removed += 1
    # authoritative sweep: catch anything the state file didn't know about (crash mid-apply)
    swept = _sweep_prefix()
    removed = max(removed, swept)
    _save_state({"active": False, "blocked": [], "rules": [], "target": None})
    return {"ok": True, "removed": removed}


def cleanup_on_start():
    """Called once at engine startup: if a previous run (or a crash) left rules behind,
    remove them so the user never boots into a silent lock. Even when the state file
    says 'clean' we still sweep by prefix (in the background, so startup isn't delayed) --
    a crash mid-apply or a lost state file could have left rules the state never recorded,
    and a silently-blocked user is far worse than a one-off background netsh call."""
    st = _load_state()
    if st.get("active") or st.get("rules"):
        return clear()
    # state says clean; verify with a NON-BLOCKING prefix sweep so a desync can't persist
    def _bg():
        if _sweep_prefix():
            _save_state({"active": False, "blocked": [], "rules": [], "target": None})
    threading.Thread(target=_bg, daemon=True).start()
    return {"ok": True, "removed": 0}


def status():
    st = _load_state()
    return {
        "build": APP_REGIONLOCK_BUILD,
        "available": available(),
        "admin": is_admin(),
        "active": bool(st.get("active")),
        "blocked": st.get("blocked") or [],
        "target": st.get("target"),
        "ruleCount": len(st.get("rules") or []),
        "whitelistTcpPorts": WHITELIST_TCP_PORTS,
    }
