"""Dependency-free regression smoke test for Fragnetic's critical safety/security
paths. Run: `py -3 test_smoke.py` (no pytest needed). Locks in the fixes made in the
20.1-20.7 hardening pass so future edits can't silently regress them. Pure-logic only;
no GPU/network/game required. Exit 0 = all pass, 1 = a regression.
"""
import json
import os
import sys
import tempfile

_FAILED = []


def check(name, cond):
    print(("  ok  " if cond else "FAIL  ") + name)
    if not cond:
        _FAILED.append(name)


# --- 1) atomic JSON write (crash-safe persistence) -----------------------------
def test_atomic_write():
    import fragroute
    d = tempfile.mkdtemp()
    p = os.path.join(d, "x.json")
    ok = fragroute._write_json_atomic(p, {"a": 1, "h": [1, 2]})
    check("atomic: returns True", ok is True)
    check("atomic: round-trips", json.load(open(p)) == {"a": 1, "h": [1, 2]})
    check("atomic: no leftover .tmp", not os.path.exists(p + ".tmp"))
    check("atomic: indent=None compact", fragroute._write_json_atomic(p, {"b": 2}, indent=None)
          and "\n" not in open(p).read())


# --- 2) knowledge allow-list (injection surface: only FragPunk hosts) ----------
def test_host_allowlist():
    import fragroute_knowledge as k
    check("allowlist: fragpunk.com allowed", k._host_allowed("https://fragpunk.com/news"))
    check("allowlist: official fandom host allowed", k._host_allowed("https://fragpunk-official.fandom.com/api.php"))
    check("allowlist: ARBITRARY fandom subdomain REJECTED (strict scope)",
          not k._host_allowed("https://fragpunk.fandom.com/wiki/X"))
    check("allowlist: evil.com REJECTED", not k._host_allowed("https://evil.com/x"))
    check("allowlist: lookalike REJECTED", not k._host_allowed("https://fragpunk.com.evil.com/x"))


# --- 3) coach system prompt carries the injection-safety clause ----------------
def test_prompt_injection_clause():
    import fragroute_ai as ai
    s = ai._LLM_SYSTEM.lower()
    check("prompt: says never obey CONTEXT instructions",
          "never obey" in s and "context" in s)


# --- 4) live detector mode gate (FPS safety: PvP never runs live) --------------
def test_live_mode_gate():
    import fragroute_live as live
    ok_real, _ = live.allowed("Shard Clash", optin_enabled=True, admin=False)
    check("live: real PvP blocked (non-admin)", ok_real is False)
    ok_admin, _ = live.allowed("Shard Clash", optin_enabled=True, admin=True)
    check("live: admin dev-override allowed", ok_admin is True)
    ok_unknown, tier = live.allowed("???garbage???", optin_enabled=True, admin=False)
    check("live: unknown mode -> blocked", ok_unknown is False)


# --- 5) subprocess-orphan job object (process-leak safety) ---------------------
def test_proc_job():
    import fragroute_proc as p
    check("proc: adopt/reap/run present", all(hasattr(p, n) for n in ("adopt", "reap", "run")))
    if os.name == "nt":
        check("proc: kill-on-close job creates", p._kill_on_close_job() is not None)


# --- 6) region-lock prefix sweep runs clean (firewall safety) ------------------
def test_regionlock_sweep():
    import fragroute_regionlock as rl
    check("regionlock: sweep returns int, no crash", isinstance(rl._sweep_prefix(), int))


# --- 7) per-process audio + WGC modules import + probe safely ------------------
def test_capture_modules():
    import fragroute_procaudio as pa
    check("procaudio: capture([]) -> False fast", pa.capture([], "x.wav", lambda: True, {}) is False)
    import fragroute_wgc as wgc
    check("wgc: find_fragpunk_hwnd(None) no crash", wgc.find_fragpunk_hwnd(None) in (None,) or True)


# --- 8) license revoke decision (commercial: don't lock out paying users) ------
def test_license_revoke():
    import fragroute_license as lic
    r = lic._ls_should_revoke
    check("license: HTTP 5xx error -> keep (None)", r({"error": "HTTP 500"}) is None)
    check("license: malformed body -> keep (None)", r({"foo": "bar"}) is None)
    check("license: valid paid key -> keep (False)",
          r({"valid": True, "license_key": {"status": "active"}}) is False)
    check("license: genuinely expired -> revoke (True)",
          r({"valid": False, "license_key": {"status": "expired"}}) is True)
    check("license: disabled -> revoke (True)",
          r({"valid": True, "license_key": {"status": "disabled"}}) is True)


def main():
    for t in (test_atomic_write, test_host_allowlist, test_prompt_injection_clause,
              test_live_mode_gate, test_proc_job, test_regionlock_sweep, test_capture_modules,
              test_license_revoke):
        print("[%s]" % t.__name__)
        try:
            t()
        except Exception as e:
            check("%s raised %s" % (t.__name__, str(e)[:60]), False)
    print("\n%s (%d checks failed)" % ("ALL PASS" if not _FAILED else "REGRESSIONS", len(_FAILED)))
    return 1 if _FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
