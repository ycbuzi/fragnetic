# FRAGROUTE — Fragpunk VPN Route Optimizer

A **local app** that controls your ProtonVPN tunnel and measures real latency so you
can hop to a populated, low-ping region and find Fragpunk matches faster.

It runs on YOUR machine (not in a browser sandbox), so it can actually read network
state and switch your route. The UI is the cyberpunk page you've seen; the engine is
a small Python server. **Stdlib only — no `pip install`.**

---

## What's real vs. estimated

| Thing | How it works |
|-------|-------------|
| **VPN switching** | REAL. Brings ProtonVPN WireGuard tunnels up/down. Bringing a tunnel up *is* the route switch. |
| **Latency / ping** | REAL. Actually pings each region's VPN endpoint. |
| **Exit IP check** | REAL. Confirms which server you're routed through. |
| **Population / heat** | ESTIMATE. Fragpunk has no public population API, so this is a time-of-day heuristic (peak 5pm–midnight local, deep pools for US-East/EU). Your own logged queue times sharpen the recommendation over time. |

---

## One-time setup

### 1. Install WireGuard
- **Windows:** https://www.wireguard.com/install/ (gives `wireguard.exe`)
- **Linux:** `sudo apt install wireguard-tools` (or `dnf`)
- **macOS:** `brew install wireguard-tools`

### 2. Get ProtonVPN WireGuard configs
ProtonVPN has no control API, so we drive WireGuard configs directly:
1. Log in at **account.protonvpn.com**
2. Go to **Downloads → WireGuard configuration**
3. Generate a config for each server you want as an option (pick a few: a US-East,
   a Frankfurt/EU, etc.)
4. Download the `.conf` files

### 3. Drop the configs in the `configs/` folder
Put both files together and create a `configs` folder next to them:

```
fragroute.py
fragroute_ui.html
configs/
   US-NY-04.conf
   DE-FRANKFURT-09.conf
   JP-TOKYO-02.conf
```

**Filename matters** — the app reads the country/state code to map each config to a
region. ProtonVPN's default names already work (`US-NY-…`, `DE-…`, `JP-…`). If a
config doesn't map, the UI shows it under "unmapped" and tells you how to rename it.

**Multiple servers per region are fully supported.** Drop as many `.conf` files as
you want for the same region — e.g. six EU servers (Frankfurt, two France, NL, etc.).
The app groups them all under that region, pings each one, and the card shows a
**server dropdown** so you can pick a specific server. The **Best Route** banner and
each card default to the lowest-ping server in the region automatically.

---

## Run it as a Windows app  (recommended)

FRAGROUTE now ships as a proper desktop app: a native window with its own icon and
a **system-tray** presence, so it sits next to Fragpunk on your second monitor. It
does **not** hook, inject into, or overlay the game, so it's fine with ACE — it's
just a separate window that switches your VPN route.

### Option A — build a one-file `FRAGROUTE.exe`  (no Python needed afterward)
1. Double-click **`build_exe.bat`**. It installs what it needs, draws the icon, and
   produces **`dist\FRAGROUTE.exe`** with a `configs\` folder beside it.
2. Move that `dist` folder wherever you like; keep `FRAGROUTE.exe` and `configs\`
   together.
3. Double-click **FRAGROUTE.exe** → click **Yes** on the UAC prompt. Done.

### Option B — run it now without building  (needs Python installed)
1. One-time: `pip install -r requirements.txt`
2. Double-click **`run_fragroute.vbs`** (launches with no console window), or run
   `python fragroute_app.py` from a terminal.

Either way you get:
- a **window** titled FRAGROUTE (not a browser tab),
- a **tray icon** — right-click for **Show / Hide**, **Always on top** (handy on a
  second monitor), **Connect lowest-ping region**, **Disconnect**, and **Quit**,
- closing the window quits the app; minimize or use **Show / Hide** to tuck it into
  the tray while you play.

If pywebview isn't installed, the app automatically opens a chromeless Edge/Chrome
window instead — everything still works, you just lose the in-window tray controls.

---

## Running the engine directly  (advanced / headless)

You can still run just the Python engine and use it in a browser tab, no app shell:

Just run it from a normal terminal — **it asks for admin/root itself:**

```bash
# Windows
python fragroute.py

# Linux / macOS
python3 fragroute.py
```

On launch it auto-elevates:
- **Windows** — a UAC prompt appears. Click **Yes**. A new elevated window opens
  with the server running; the original window closes.
- **Linux / macOS** — it re-runs under `sudo` and asks for your password in the
  same terminal.

You no longer need to manually open an "as Administrator" terminal first. (You
still can if you prefer — if you're already elevated, it just runs in place with
no prompt.)

Your browser opens to the dashboard automatically.

### Try it safely first
Run with `--dry-run` to see exactly what it *would* do without touching your
network. **Dry-run skips elevation entirely** (it doesn't need admin), so there's
no UAC/sudo prompt:

```bash
python fragroute.py --dry-run
```

In dry-run the UI works fully, shows the WireGuard commands it would run, and tracks
state — but never executes a tunnel change. Good for a first look.

### Options
```
python fragroute.py --dry-run        # simulate, never execute tunnel changes (no elevation)
python fragroute.py --no-elevate     # don't auto-request admin; run exactly as launched
python fragroute.py --port 8787      # use a different port (default 8765)
python fragroute.py --configs PATH   # configs folder somewhere else
python fragroute.py --no-browser     # don't auto-open the browser
```

If you decline the UAC prompt (or use `--no-elevate` without already being admin),
the app still runs but the status bar shows **NO ADMIN** and tunnel switching is
disabled — pings and the UI still work.

---

## Using the dashboard

1. **Refresh Pings** — measures real latency to each region's Proton endpoint.
2. **Max ping slider** — filters the recommendation to servers under your cap.
3. The **Best Route** banner picks the lowest expected-queue region under your cap.
   Expected queue = heat estimate blended with your own logged data.
4. Hit **Connect** on a card — the app raises that region's WireGuard tunnel and the
   status bar turns green with the active tunnel + route. It also auto-starts a queue
   timer for that region. If a region has several servers, use the **server dropdown**
   on the card to pick which one (it defaults to the lowest-ping server, and each
   option shows its live ping). The Best Route banner always connects the region's
   current best server.
5. In Fragpunk, change the in-game server (the **R** key on the Play screen) to match,
   then queue up.
6. When you get a match, hit **Match Found** — it logs the queue time so the
   recommendation gets smarter. **Cancel** logs a bailed queue.
7. **Verify Exit IP** confirms you're actually routed through the Proton server.
8. **Disconnect** drops the tunnel back to your normal connection.

Switching regions auto-drops the previous tunnel first, so you never stack two.

---

## Notes & limits

- The app binds to `127.0.0.1` only — nothing is exposed to your network.
- It never stores your ProtonVPN password; it only uses the WireGuard `.conf` files
  you place in the folder, which contain per-server keys, not your account login.
- If you see **NO ADMIN** in the status bar, tunnel switching is disabled — restart
  the terminal as admin/sudo.
- If you see **WireGuard NOT FOUND**, install it and make sure it's on your PATH
  (Windows: the installer's default location is auto-detected).
- Population is a heuristic. It can't see live player counts because Fragpunk doesn't
  publish them. The longer you log queues, the more the recommendation leans on your
  real history instead of the estimate.
