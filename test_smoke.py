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


# --- 9) public-IP classifier (region-lock carve-out: never block the game) -----
def test_public_ip():
    import fragroute
    check("ip: public game server -> True", fragroute._is_public_ip("8.221.52.114"))
    check("ip: LAN 192.168 -> False", not fragroute._is_public_ip("192.168.0.21"))
    check("ip: loopback -> False", not fragroute._is_public_ip("127.0.0.1"))
    check("ip: CGNAT 100.64 -> False", not fragroute._is_public_ip("100.90.1.1"))


# --- 10) update version compare (don't nag/downgrade wrongly) -------------------
def test_ver_tuple():
    import fragroute
    vt = fragroute._ver_tuple
    check("ver: 20.7 > 20.6", vt("20.7") > vt("20.6"))
    check("ver: 20.10 > 20.9 (numeric, not lexical)", vt("20.10") > vt("20.9"))
    check("ver: equal", vt("20.7") == vt("20.7"))


# --- 11) text-mode subprocess must decode safely (no UnicodeDecodeError reader-thread crash) --
def test_subprocess_decode_safe():
    # A helper/CLI (netsh, tasklist, ffmpeg, sd-cli, llama-server...) can emit bytes the locale
    # codec (cp1252) can't decode. With text=True and STRICT errors the subprocess reader thread
    # dies with UnicodeDecodeError and the captured output is lost -- a recurring diag-log crash.
    # Every text-mode call must set errors= (or route through _proc.run, which injects it).
    import glob
    here = os.path.dirname(os.path.abspath(__file__))
    bad = []
    for fp in glob.glob(os.path.join(here, "*.py")):
        if os.path.basename(fp) == "test_smoke.py":
            continue
        lines = open(fp, encoding="utf-8").read().splitlines()
        for i, ln in enumerate(lines):
            if "text=True" in ln or "universal_newlines=True" in ln:
                window = "\n".join(lines[max(0, i - 2):i + 3])   # the call can span a couple lines
                if "errors=" not in window and "_proc.run" not in window:
                    bad.append("%s:%d" % (os.path.basename(fp), i + 1))
    check("subprocess: every text=True call sets errors= (%s)" % (", ".join(bad) or "clean"), not bad)
    import fragroute_proc as proc
    import subprocess
    seen = {}
    real = subprocess.Popen

    class _Probe:
        def __init__(self, args, **kw):
            seen.update(kw)
            raise RuntimeError("halt-before-spawn")
    subprocess.Popen = _Probe
    try:
        try:
            proc.run(["noop"], capture_output=True, text=True)
        except Exception:
            pass
    finally:
        subprocess.Popen = real
    check("proc.run(text=True) injects errors=replace", seen.get("errors") == "replace")


# --- 12) Ollama coach backend: model auto-pick + active gating (no network) -------------------
def test_ollama_backend():
    import fragroute_llm as L
    _orig = L._ollama_probe
    L._ollama_probe = lambda force=False: L.OLLAMA["up"]   # avoid a real network probe in the test
    try:
        L.configure_ollama(enabled=True, model="")
        L.OLLAMA["up"] = True
        L.OLLAMA["models"] = ["nomic-embed-text:latest", "qwen2.5:14b"]
        check("ollama: auto-pick skips the embedding model", L._ollama_model() == "qwen2.5:14b")
        L.configure_ollama(model="qwen2.5:32b")
        check("ollama: an explicit model is honored", L._ollama_model() == "qwen2.5:32b")
        L.configure_ollama(enabled=False)
        check("ollama: disabled -> not active (falls back to bundled)", L._ollama_active() is False)
        L.configure_ollama(enabled=True)
        check("ollama: enabled + up + model -> active", L._ollama_active() is True)
        L.OLLAMA["models"] = []
        L.configure_ollama(model="")
        check("ollama: up but no usable model -> not active", L._ollama_active() is False)
    finally:
        L._ollama_probe = _orig
        L.OLLAMA["up"] = False
        L.configure_ollama(enabled=True, model="")


# --- 13) semantic RAG: cosine ranking + keyword fallback (no network) -------------------------
def test_semantic_rag():
    import fragroute_learning as L
    # fake embedder: encodes MEANING (not literal words) into 2 axes, so a query with no shared
    # words still ranks the semantically-matching fact first -- the whole point of embeddings.
    def emb(texts):
        def v(t):
            t = (t or "").lower()
            revive = any(k in t for k in ("revive", "life saver", "bring back", "dead teammate", "downed"))
            plant = any(k in t for k in ("plant", "converter", "attack"))
            return [1.0 if revive else 0.0, 1.0 if plant else 0.0, 0.1]
        return [v(t) for t in texts]
    facts = [("shard_clash", {"trust": "official", "source": "x"}, "The Life Saver card can revive a downed ally."),
             ("shard_clash", {"trust": "wiki", "source": "y"}, "Attackers plant the Converter to win the round.")]
    L._FACT_EMB.clear()
    res = L._semantic_facts("how do I bring back a dead teammate", facts, 2, emb)
    check("semantic: revive query ranks the Life Saver fact first (no shared words)",
          bool(res) and "Life Saver" in res[0]["fact"])
    check("semantic: fact vectors got cached", len(L._FACT_EMB) == 2)
    check("semantic: no-embed -> None (falls back to keyword)",
          L._semantic_facts("x", facts, 2, lambda t: None) is None)
    L._FACT_EMB.clear()


def main():
    for t in (test_atomic_write, test_host_allowlist, test_prompt_injection_clause,
              test_live_mode_gate, test_proc_job, test_regionlock_sweep, test_capture_modules,
              test_license_revoke, test_public_ip, test_ver_tuple, test_subprocess_decode_safe,
              test_ollama_backend, test_semantic_rag):
        print("[%s]" % t.__name__)
        try:
            t()
        except Exception as e:
            check("%s raised %s" % (t.__name__, str(e)[:60]), False)
    print("\n%s (%d checks failed)" % ("ALL PASS" if not _FAILED else "REGRESSIONS", len(_FAILED)))
    return 1 if _FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
