# Fragnetic — your FragPunk companion

A **local companion app for [FragPunk](https://fragpunk.com)** that runs entirely on your
PC. It gives you a private AI coach, records and reviews your matches, and tells you the
real ping to every region so you can pick the best one — no cloud account required to try it.

> **Download:** [**Fragnetic-Setup.zip**](https://github.com/ycbuzi/fragnetic/releases/latest/download/Fragnetic-Setup.zip)
> · Site: [ycbuzi.github.io/fragnetic](https://ycbuzi.github.io/fragnetic/)

It runs on **your machine** (not a browser sandbox), so it can read network state and see
the game — but it does **not** hook, inject into, or overlay FragPunk. It's a separate
window that sits next to the game.

---

## What it does

| Feature | What you get |
|---|---|
| **AI Coach** | A private, on-device coach — **chat, voice, and vision** (it can read your maps/scoreboards). Ask about weapons, economy, lancers, maps, or your own play. Runs on bundled local models, or point it at **your own [Ollama](https://ollama.com) models** for a bigger brain. Grounded in FragPunk knowledge; nothing is uploaded. |
| **Recording & review** | Automatic gameplay capture (rolling buffer + full-match), **auto-highlights** (it finds your action moments and stitches a montage), and AI review of your clips. Hardware-accelerated on NVIDIA, AMD, and Intel GPUs. |
| **Region intelligence** | Measures your **true ping to every region** and shows which server region you're actually playing on — so you can pick the best region in-game with real numbers, not guesses. Optional **VPN region routing** with a **FragPunk-only split tunnel** (only the game rides the VPN; your browser/Discord stay full-speed) and a no-VPN **region lock**. |
| **AI image tools** | Local image generation (Pro). |
| **Locker & more** | Auto-cropped skin gallery, per-lancer profiles, health diagnostics. |

**Free to try** with a 14-day Pro preview. Pro features unlock with a license key.

---

## Install (Windows)

1. **Download** [`Fragnetic-Setup.zip`](https://github.com/ycbuzi/fragnetic/releases/latest/download/Fragnetic-Setup.zip), unzip it anywhere, and run **`Fragnetic.exe`**.
2. Windows may show a blue **"Windows protected your PC"** screen — that's just because the
   app is new and not yet code-signed. Click **More info → Run anyway**.
3. Click **Yes** on the admin prompt (needed to read your network so it can measure real
   region ping). On first launch it scans your hardware and downloads the AI models that
   fit your GPU.

The app self-updates its *notice*: when a newer build ships, it tells you in-app.

---

## About regions (the honest version)

FragPunk decides your match region **server-side, from your connection** — and the game
already lets you pick a region in its own **in-game server menu** (the `R` key on the Play
screen). Fragnetic's job is to make that choice *informed*: it pings every region's real
servers and tells you which one actually gives you the best latency, and it reads which
region you're truly playing on from the live match.

If you want to *improve* your ping to a **far** region, you can drop in your own WireGuard
`.conf` (from any provider) and Fragnetic will route through it — optional, for power users.
Two things make this nicer than a normal VPN:

- **FragPunk-only split tunnel:** route *only* the game through the VPN, so your browser,
  Discord, and downloads stay on your normal connection at full speed.
- **No-VPN region lock:** or skip the VPN entirely and nudge matchmaking by firewall-blocking
  the regions you don't want (whitelisting the game's lobby/anti-cheat so nothing breaks).

There's no built-in VPN requirement and no subscription tied to the app.

---

## Privacy

- Runs **100% locally** — the AI models, coaching, and recording all happen on your PC.
- The app binds to `127.0.0.1` only; nothing is exposed to your network.
- No account is required to try it. Your account and license (if any) stay on your machine.

---

## Building from source

This repo is public so it can host the [landing page](https://ycbuzi.github.io/fragnetic/).
The app is a Python engine (stdlib-only server) plus a WebView2 native window. To build the
one-file exe yourself, run **`build_exe.bat`** on Windows (produces `dist\Fragnetic.exe`).

---

## Disclaimer

Fragnetic is an **independent, unofficial** companion app for FragPunk. It is not affiliated
with, sponsored by, or endorsed by NetEase. "FragPunk" is a trademark of its respective
owner, referenced here only to describe compatibility. Use of any third-party software with
FragPunk is at your own risk under FragPunk's own Terms of Service — see the
[EULA](https://ycbuzi.github.io/fragnetic/EULA.html) and
[Privacy Policy](https://ycbuzi.github.io/fragnetic/PRIVACY.html).
