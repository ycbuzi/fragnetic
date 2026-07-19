## Fragnetic 20.25 — Linux polish + any-browser + honest VPN wording

Fixes from real Linux tester feedback, plus clearer VPN messaging. Ships to **both** Windows
and Linux.

### Fixed
- **Game now shows in System Health on Linux.** The Health tab used a Windows-only process check
  that always reported "not running" off Windows, so the game appeared on the home page but not in
  Health. Both now use the same cross-platform detection.
- **In-app browser works with ANY browser.** Was effectively Chrome-only. Now: Chromium family
  (Chrome/Edge/Brave/Vivaldi/Chromium) → ephemeral private window; Firefox family
  (Firefox/ESR/LibreWolf/Waterfox) → private window; and if neither is found it opens your
  **default browser** — so any installed browser works.
- **ffmpeg capture pump** always closes stdin (can't leave the encoder hung).

### Changed
- **VPN wording is now honest and provider-neutral.** A VPN is **optional** (real region ping and
  region-lock work without one), and if you do want VPN routing, **any WireGuard provider works** —
  Proton, Mullvad, IVPN, Windscribe, etc. Removed the old "ProtonVPN .conf files" labels that made
  it look like ProtonVPN specifically was required. The "get a free config" guide still points to
  ProtonVPN's free tier as one easy option (and notes any provider works).

### Linux
- Native **Linux build** available (`Fragnetic-linux-x86_64.tar.gz`) — run `./install.sh` for an
  app-menu entry + icon + its own window. Built + smoke-tested + privacy-scanned in CI.

Update by downloading the new asset for your platform below.
