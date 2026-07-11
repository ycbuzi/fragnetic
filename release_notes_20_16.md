## Fragnetic 20.16 — FragPunk-only VPN (split tunnel)

You can now route **only FragPunk through the VPN** — your browser, Discord, downloads and
everything else stay on your normal connection. No FPS impact.

### New
- **FragPunk-only VPN (split tunnel)** toggle in Settings → VPN. When on, connecting a route
  installs a variant of your WireGuard config whose `AllowedIPs` covers **only FragPunk's
  server ranges** (the same curated + learned ranges the region lock uses), instead of the
  usual full-tunnel `0.0.0.0/0`. So:
  - FragPunk's traffic → through the VPN (region switched, as before)
  - Everything else → your normal connection (no slowdown, no region change for browsing)
- The tunnel's **DNS is left untouched** in this mode, so non-FragPunk name resolution is
  unaffected.
- Safe by design: if the split config can't be built (e.g. no server ranges learned yet), it
  automatically falls back to the normal full tunnel. Off by default.

### Good to know
- Coverage grows as you play — the split routes your seeded regions plus every server /24 the
  app has learned from real matches. If FragPunk ever uses a brand-new datacenter you haven't
  hit yet, that path would briefly bypass the tunnel until it's learned (same basis as the
  region lock).
- Takes effect on your next connect after enabling it.

Update by downloading Fragnetic-Setup.zip below.
