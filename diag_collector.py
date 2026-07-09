#!/usr/bin/env python3
"""Diagnostic collector: polls the running FRAGROUTE app's API + FragPunk's real
connections, logs snapshots + every state change, and flags discrepancies
(detection lag, region mismatch, phantom matches, route-optimizer results,
mid-match VPN-switch handling). Read the log to analyze a live session."""
import json, subprocess, time, urllib.request, sys

LOG = sys.argv[1] if len(sys.argv) > 1 else "diag.log"
WEB = {"80", "443", "8080", "8443"}
GAME = ("fragpunk.exe", "fragpunk-win64-shipping.exe", "fragpunk_launcher.exe")


def out(s):
    line = time.strftime("%H:%M:%S") + " " + s
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def get(port, path, t=5):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=t) as r:
            return json.loads(r.read())
    except Exception:
        return None


def find_port():
    for p in range(8765, 8786):
        if get(p, "/api/status", 2):
            return p
    return None


def is_private(ip):
    try:
        a, b = (int(x) for x in ip.split(".")[:2])
    except Exception:
        return True
    return (a in (10, 127, 0) or (a == 192 and b == 168) or
            (a == 172 and 16 <= b <= 31) or (a == 169 and b == 254) or
            (a == 100 and 64 <= b <= 127) or a >= 224)


def game_conns():
    try:
        tl = subprocess.run(["tasklist", "/fo", "csv", "/nh"],
                            capture_output=True, text=True, errors="replace").stdout
        pids = set()
        for line in tl.splitlines():
            c = [x.strip('" ') for x in line.split('","')]
            if len(c) >= 2 and c[0].lower() in GAME and c[1].isdigit():
                pids.add(c[1])
        if not pids:
            return None, []
        ns = subprocess.run(["netstat", "-ano"], capture_output=True, text=True, errors="replace").stdout
        est = []
        for line in ns.splitlines():
            p = line.split()
            if len(p) >= 5 and p[0] == "TCP" and p[-1] in pids and p[3] == "ESTABLISHED":
                host, _, port = p[2].rpartition(":")
                if "." in host and not is_private(host) and port not in WEB:
                    est.append(f"{host}:{port}")
        return True, sorted(est)
    except Exception:
        return None, []


def snap(port):
    return {
        "status": get(port, "/api/status") or {},
        "game": get(port, "/api/game") or {},
        "ad": get(port, "/api/autodetect") or {},
        "scout": get(port, "/api/scout") or {},
        "route": get(port, "/api/route/profile") or {},
        "log": get(port, "/api/log") or {},
    }


def main():
    open(LOG, "w").close()
    out("=== diagnostic collector start ===")
    port = None
    while port is None:
        port = find_port()
        if port is None:
            time.sleep(3)
    st = get(port, "/api/status") or {}
    out(f"APP on :{port}  build={st.get('build')} admin={st.get('admin')} "
        f"dryRun={st.get('dryRun')} wireguard={st.get('wireguard')}")

    last = {}
    last_events = []
    full_at = 0
    while True:
        try:
            s = snap(port)
            running, est = game_conns()
            g, ad, sc, rt = s["game"], s["ad"], s["scout"], s["route"]
            srv = (g.get("server") or {})
            lob = (g.get("lobby") or {})
            mp = (sc.get("matchPing") or {})
            pop = (sc.get("population") or {})

            cur = {
                "phase": ad.get("phase"),
                "gstate": g.get("state"),
                "srv": srv.get("ip"), "srvRid": srv.get("regionId"),
                "tunnel": s["status"].get("activeTunnel"),
                "logN": len(s["log"].get("log", [])),
                "routeRun": rt.get("running"),
                "best": sc.get("best"),
            }
            # log every CHANGE in key fields
            for k, v in cur.items():
                if last.get(k) != v:
                    out(f"Δ {k}: {last.get(k)} -> {v}")
            # new autodetect events
            evs = ad.get("events", [])
            new = [e for e in evs if e not in last_events]
            for e in reversed(new):
                extra = {kk: e[kk] for kk in e if kk not in ("kind", "ts")}
                out(f"  EVENT {e.get('kind')} {extra}")
            last_events = evs

            # route optimizer progress / results
            if rt.get("running"):
                out(f"  ROUTE {rt.get('done')}/{rt.get('total')} testing {rt.get('current')}")
            elif rt.get("results") and last.get("routeRun"):  # just finished
                best = rt.get("best") or {}
                out(f"  ROUTE DONE best={best.get('route')} {best.get('pingMs')}ms ; "
                    f"top: " + ", ".join(f"{r['route']}={r['pingMs']}" for r in rt["results"][:5]))

            # CROSS-CHECKS (flag discrepancies)
            if running and est:
                # detector says match server X; is X actually an established conn?
                if cur["phase"] == "match" and cur["srv"] and cur["srv"] not in [e.split(":")[0] for e in est]:
                    out(f"  ⚠ detector match-server {cur['srv']} not in live conns {est}")
                if cur["phase"] == "menu" and len([e for e in est]) > 1:
                    out(f"  ⚠ phase=menu but {len(est)} non-lobby conns up: {est}")
            if mp.get("ms") is not None:
                out(f"  matchping {mp.get('ms')}ms avg{mp.get('avg')} jit{mp.get('jitter')} loss{mp.get('lossPct')}%")

            # periodic full snapshot every 30s
            now = time.time()
            if now - full_at > 30:
                full_at = now
                out(f"SNAP phase={cur['phase']} gstate={cur['gstate']} "
                    f"srv={cur['srv']}({cur['srvRid']}) lobby={lob.get('ip')}({lob.get('regionId')},{lob.get('pingMs')}ms) "
                    f"tunnel={cur['tunnel']} best={cur['best']} "
                    f"pop={pop.get('current')}(peak{pop.get('recentPeak')},x{pop.get('mult')}) "
                    f"logN={cur['logN']} conns={est}")
            last = cur
        except Exception as e:
            out(f"(collector error: {e})")
        time.sleep(4)


if __name__ == "__main__":
    main()
