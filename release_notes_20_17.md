## Fragnetic 20.17 — split-tunnel bandwidth fix, dead-tunnel detection, auto-highlights fix

Three fixes from live diagnosis. No FPS impact.

### Fixed
- **Split tunnel no longer throttles your other traffic.** It was routing broad `/16` Google
  Cloud + Alibaba ranges (~400,000 IPs) through the VPN, dragging unrelated services onto the
  tunnel. Now it routes only your real FragPunk servers + lobby (~7,680 IPs) — browser,
  Discord, downloads and anything else on those clouds stay full-speed on your normal line.
  Region-switching is preserved (matchmaking still tunnels).
- **Dead-tunnel detection.** After connecting, the app now verifies the WireGuard handshake
  actually completed. If a tunnel installs but never establishes (server down / bad config),
  it's **torn down and reported as failed** — instead of silently black-holing FragPunk's
  traffic into a dead tunnel and claiming "connected."
- **Auto-highlights / Video tab "missing ffmpeg" fixed.** A startup race left the video engine
  without its ffmpeg path (even though the recorder had it). The video tools now find ffmpeg
  themselves at use-time, so highlights and editing work reliably.

Update by downloading Fragnetic-Setup.zip below and replacing your existing folder.
