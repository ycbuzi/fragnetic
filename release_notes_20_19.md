## Fragnetic 20.19 — first-run System Check

A one-glance "will this run well on my PC?" readout, so you know what's supported before you
commit. No FPS impact.

### New — System Check
On the **Setup** tab (top of *Your PC & compatibility*) there's now a **System Check** card with a
plain-English go/no-go verdict and a per-item breakdown:

- **Windows 10/11 (x64)** — OS + build
- **Graphics / AI model** — your GPU(s) + VRAM, and the exact coach model that fits (14B / Phi-3.5 /
  CPU-friendly)
- **Memory** — installed RAM
- **App window (WebView2)** — whether the Edge WebView2 runtime is present (if not, the app just
  opens in your browser)
- **Recording** — ffmpeg + which hardware encoder will be used (NVENC / AMF / QSV / software)
- **VPN routing (WireGuard)** — installed or not (it's optional — ping + region-lock still work
  without it)
- **Admin rights** — elevated or not (some network features need admin)
- **Free disk** — headroom for the downloadable AI models

Each row is green / amber / blue, and missing pieces just note the fallback instead of blocking you.
Backend: `GET /api/syscheck` (`system_check()`); re-run any time with the ↻ button.

Update by downloading Fragnetic-Setup.zip below and replacing your existing folder.
