## Fragnetic 20.18 — QoL tools + highlight playback fix

Quality-of-life additions and a fix so your highlight videos actually play. No FPS impact.

### New — quality of life
- **⚡ Verify VPN** — the header button now runs a full reality-check: your real exit IP + where
  it geolocates, a **through-tunnel liveness probe** (catches a dead tunnel that's black-holing
  FragPunk), and split-tunnel status + how many FragPunk ranges are routed. One click instead of
  digging through Resource Monitor.
- **⚡ Connect Best** — new header button: connect the lowest-ping region in one click.
- **Start with Windows** — a toggle in *Settings → Routing & VPN* to launch Fragnetic at login
  (via a Scheduled Task, so there's no UAC prompt every boot).

### Fixed
- **Highlight / edited videos wouldn't play.** The Video-tab outputs (auto-highlights, montages,
  trims) were written with the mp4 index (`moov` atom) at the **end**, which the in-app video
  player can't read. All video outputs now use **faststart** (index at the front) so they play
  immediately. Any highlights you already made are repaired too.

Update by downloading Fragnetic-Setup.zip below and replacing your existing folder.
