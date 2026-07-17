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


# --- 14) FragPunk-only split tunnel: .conf rewrite (no network) -------------------------------
def test_split_tunnel_conf():
    import fragroute as F
    import tempfile
    from pathlib import Path
    j = Path(tempfile.mkdtemp()) / "servers.json"
    j.write_text('{"regions": {}}')     # empty learned set -> seed CIDRs still apply
    F.SERVERS_PATH = j
    cidrs = F.fragpunk_route_cidrs()
    check("split: fragpunk_route_cidrs returns seed ranges", len(cidrs) >= 2)
    check("split: never contains the default route", "0.0.0.0/0" not in cidrs)
    # never route broad /16 shared-cloud ranges through the VPN (that throttled non-FragPunk traffic)
    import ipaddress as _ip
    broad = [c for c in cidrs if _ip.ip_network(c, strict=False).prefixlen <= 16]
    check("split: no broad /16 shared-cloud ranges routed (%s)" % (broad or "clean"), not broad)
    conf = Path(tempfile.mkdtemp()) / "wg-DE-1.conf"
    conf.write_text("[Interface]\nPrivateKey = K\nAddress = 10.2.0.2/32\nDNS = 10.2.0.1\n\n"
                    "[Peer]\nPublicKey = P\nEndpoint = 9.9.9.9:51820\nAllowedIPs = 0.0.0.0/0, ::/0\n")
    sp = F._fragonly_conf({"path": str(conf), "name": "wg-DE-1"})
    txt = Path(sp).read_text() if sp else ""
    check("split: written with the SAME stem (tunnel name unchanged)", bool(sp) and Path(sp).name == "wg-DE-1.conf")
    check("split: full-tunnel 0.0.0.0/0 removed", bool(txt) and "0.0.0.0/0" not in txt)
    check("split: DNS line dropped (system resolver untouched)", bool(txt) and "DNS" not in txt)
    check("split: VPN endpoint preserved", "9.9.9.9" in txt)
    check("split: AllowedIPs narrowed to FragPunk ranges", "AllowedIPs = " in txt and ("8.221" in txt or "8.211" in txt))


# --- 15) QoL: vpn_verify + connect_best_region graceful paths (no network) --------------------
def test_qol():
    import fragroute as F
    F.STATE["active_tunnel"] = None
    v = F.vpn_verify()
    check("qol: vpn_verify with no tunnel -> verdict 'off'", v.get("verdict") == "off" and v.get("connected") is False)
    F.STATE["configs"] = {}
    b = F.connect_best_region()
    check("qol: connect_best_region with no configs -> graceful ok=False", b.get("ok") is False and "region" in b.get("message", "").lower())
    check("qol: startWithWindows default present", "startWithWindows" in F.DEFAULT_SETTINGS)


# --- 16) video outputs must be faststart (moov at front) or the WebView player can't play them --
def test_video_faststart():
    here = os.path.dirname(os.path.abspath(__file__))
    src = open(os.path.join(here, "fragroute_video.py"), encoding="utf-8").read()
    check("video: final outputs use +faststart (%d found)" % src.count('"+faststart"'),
          src.count('"+faststart"') >= 4)
    check("video: montage no-title path re-muxes (no raw shutil.copy2 that leaves moov at end)",
          "shutil.copy2(concat, out)" not in src)


# --- 23) Linux import-safety: no unguarded module-level Windows-only imports ------------------
def test_linux_import_safe():
    import glob, re as _re
    here = os.path.dirname(os.path.abspath(__file__))
    offenders = []
    for path in glob.glob(os.path.join(here, "fragroute*.py")):
        src = open(path, encoding="utf-8", errors="replace").read()
        for m in _re.finditer(r"(?m)^(import winreg|from ctypes import wintypes|import msvcrt)\b", src):
            # allowed only if the very lines above open a try: block (guarded import)
            head = src[:m.start()].rstrip().splitlines()
            guarded = bool(head) and head[-1].strip().endswith("try:")
            if not guarded:
                offenders.append(os.path.basename(path) + ": " + m.group(0))
    check("linux: no UNGUARDED module-level Windows-only imports (%s)" % (offenders or "clean"),
          not offenders)
    check("linux: fragroute_app.py guards its ctypes/wintypes import",
          "except Exception:" in open(os.path.join(here, "fragroute_app.py"), encoding="utf-8", errors="replace").read().split("_GWL_STYLE")[0])


# --- 22) packaging must NEVER bundle personal data into the shipped exe/release ---------------
def test_ship_privacy():
    here = os.path.dirname(os.path.abspath(__file__))
    be = open(os.path.join(here, "build_exe.bat"), encoding="utf-8", errors="replace").read()
    check("privacy: build does NOT bundle the runtime owned-skins file",
          "add-data dist\\fragroute_weapon_skins.json" not in be)
    check("privacy: build does NOT bundle the raw runtime icons (custom wallpaper)",
          "add-data dist\\fragroute_icons.json" not in be)
    check("privacy: build bundles the SANITIZED reference icons instead",
          "ship_assets\\fragroute_icons.json" in be and "sanitize_ship_assets.py" in be)
    pr = open(os.path.join(here, "package_release.bat"), encoding="utf-8", errors="replace").read()
    check("privacy: release ships sanitized icons + scans for a bare wallpaper leak",
          "ship_assets\\fragroute_icons.json" in pr and "wallpaper" in pr)
    # the sanitized reference file itself must never contain a bare 'wallpaper' slot
    sp = os.path.join(here, "ship_assets", "fragroute_icons.json")
    if os.path.exists(sp):
        import json as _j
        ks = (_j.load(open(sp, encoding="utf-8")).get("slots") or {})
        check("privacy: sanitized ship icons carry no bare 'wallpaper' slot", "wallpaper" not in ks)


# --- 21) coach must be grounded in the REAL lancer roster + forbidden from inventing names -----
def test_coach_lancer_grounding():
    here = os.path.dirname(os.path.abspath(__file__))
    src = open(os.path.join(here, "fragroute_ai.py"), encoding="utf-8").read()
    roster = ["Broker", "Nitro", "Hollowpoint", "Jaguar", "Chum", "Corona", "Serket",
              "Pathojen", "Zephyr", "Spider", "Kismet", "Axon", "Sonar"]
    check("coach: system prompt lists the real 13-lancer roster",
          all(name in src for name in roster))
    check("coach: system prompt forbids inventing/guessing lancer names",
          "NEVER name, invent" in src and ("do NOT guess" in src or "not certain" in src))


# --- 20) VPN framing: region-lock marked experimental + one-click free-config import ----------
def test_vpn_accessibility():
    here = os.path.dirname(os.path.abspath(__file__))
    html = open(os.path.join(here, "fragroute_ui.html"), encoding="utf-8").read()
    check("vpn: region-lock marked EXPERIMENTAL", "xp-badge" in html and ">EXPERIMENTAL<" in html)
    check("vpn: free-config guide modal present", 'id="vpnGuideOv"' in html and "Get a free WireGuard config" in html)
    check("vpn: one-click .conf import wired", 'id="vpnConfFile"' in html and "/api/configs/import" in html)
    check("vpn: framing says VPN optional / ping+lock work without it",
          "You don't need a VPN" in html or "optional · only to improve ping" in html)
    import fragroute as F
    src = open(os.path.join(here, "fragroute.py"), encoding="utf-8").read()
    check("vpn: import endpoint validates WireGuard + sanitizes filename",
          '"/api/configs/import"' in src and "[Interface]" in src and "[Peer]" in src)
    check("vpn: open-configs-folder endpoint present", '"/api/configs/open"' in src)


# --- 19) first-run Getting Started checklist wired (onboarding a new buyer through setup) ------
def test_getting_started():
    here = os.path.dirname(os.path.abspath(__file__))
    html = open(os.path.join(here, "fragroute_ui.html"), encoding="utf-8").read()
    check("getstarted: showGetStarted() overlay exists", "function showGetStarted(" in html)
    check("getstarted: gated once via getStartedDone + auto-fires on first run",
          "getStartedDone" in html and "maybeGetStarted" in html)
    check("getstarted: pulls live PC + model status", "/api/syscheck" in html and "/api/setup/models" in html)
    check("getstarted: re-runnable from Setup tab", 'id="setGetStarted"' in html)
    check("getstarted: existing users not re-onboarded", "SETTINGS.welcomeDone){ SETTINGS.getStartedDone" in html)
    import fragroute as F
    check("getstarted: server default persisted", "getStartedDone" in F.DEFAULT_SETTINGS)


# --- 18) owner/admin-only chrome must be gated (build number + dev readouts hidden from buyers) --
def test_admin_gating():
    here = os.path.dirname(os.path.abspath(__file__))
    html = open(os.path.join(here, "fragroute_ui.html"), encoding="utf-8").read()
    check("admin-gate: CSS hides [data-admin-only] unless body.admin-mode",
          "body:not(.admin-mode) [data-admin-only]" in html)
    check("admin-gate: header build tag is admin-only",
          'id="buildTag" data-admin-only' in html)
    check("admin-gate: dev launch-flag readouts (port/dry-run) are admin-only",
          html.count("set-row\" data-admin-only") >= 2 or html.count('set-row" data-admin-only') >= 2)
    check("admin-gate: admin-mode is toggled from entitlement tier",
          "classList.toggle('admin-mode'" in html and "tier === 'admin'" in html)


# --- 17) first-run system check aggregates readiness items with per-item verdicts ------------
def test_syscheck():
    import fragroute as F
    r = F.system_check()
    check("syscheck: ok + items list", r.get("ok") is True and isinstance(r.get("items"), list) and len(r["items"]) >= 6)
    check("syscheck: every item has label/status/detail",
          all(i.get("label") and i.get("status") in ("good", "warn", "bad", "info") and "detail" in i for i in r["items"]))
    labels = " ".join(i["label"].lower() for i in r["items"])
    check("syscheck: covers the core surfaces (webview2, recording, wireguard, admin, disk)",
          all(k in labels for k in ("webview2", "recording", "wireguard", "admin", "disk")))
    check("syscheck: ready reflects zero bad items",
          r.get("ready") == (sum(1 for i in r["items"] if i["status"] == "bad") == 0))


def main():
    for t in (test_atomic_write, test_host_allowlist, test_prompt_injection_clause,
              test_live_mode_gate, test_proc_job, test_regionlock_sweep, test_capture_modules,
              test_license_revoke, test_public_ip, test_ver_tuple, test_subprocess_decode_safe,
              test_ollama_backend, test_semantic_rag, test_split_tunnel_conf, test_qol,
              test_video_faststart, test_syscheck, test_admin_gating, test_getting_started,
              test_vpn_accessibility, test_coach_lancer_grounding, test_ship_privacy,
              test_linux_import_safe):
        print("[%s]" % t.__name__)
        try:
            t()
        except Exception as e:
            check("%s raised %s" % (t.__name__, str(e)[:60]), False)
    print("\n%s (%d checks failed)" % ("ALL PASS" if not _FAILED else "REGRESSIONS", len(_FAILED)))
    return 1 if _FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
