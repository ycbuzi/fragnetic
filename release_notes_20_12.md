## Fragnetic 20.12 — region-accuracy fixes (from live-match diagnostics)

Mining a live match session surfaced two real data-quality bugs. No FPS impact.

### Fixed
- **Server region misattribution (Route Optimizer accuracy).** Match servers were being
  learned under *the VPN region you queued from* instead of *where the server physically
  is*. That split real datacenters across regions — e.g. a Frankfurt /24 with one IP in EU
  and its sibling in US-West depending on which tunnel you used, which is impossible for a
  single datacenter and quietly corrupted the optimizer's ping map. Servers are now learned
  by their **physical GeoIP region**, an IP can only live in **one** region (stale copies
  are purged), and existing mislabels are auto-corrected. Your `us-west` bucket's Frankfurt
  IP was relocated to `eu`.
- **Learning counted queue-dodges as matches.** Sub-45s "matches" (dodges / login flaps —
  seen live as 22s/41s/21s) were logged to your learning stats and triggered a wasted
  post-match rank read, even though the recorder correctly skips them. Both now use the
  **same 45s floor** the recorder does.

### Still investigating (needs a live look, not shipped yet)
- Match **mode** frequently logging as `unknown` (even a 27-min match) — the mode OCR looks
  like it reads once at the match-start edge before the mode is on-screen. Fixing it right
  needs a live OCR check rather than a blind guess.

Update by downloading Fragnetic-Setup.zip below and replacing your existing folder.
