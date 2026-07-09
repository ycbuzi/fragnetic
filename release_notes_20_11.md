## Fragnetic 20.11 — medium/low robustness sweep

A follow-up polish pass across the whole app (the tier below the crash/data-loss fixes
already shipped in 20.9/20.10). No FPS impact.

### Fixed
- **Malformed request handling** — numeric API inputs (image-gen steps/size/seed, video
  trim, clip length, fps, persona tuning) now coerce safely instead of throwing on a bad
  value, so a stray input can't 500 an endpoint or inflate the health error count.
- **Thread hygiene** — the in-game server-ping scout now uses daemon threads, so a hung
  ping can't block app exit or pile up over a session (matches the earlier latency fix).
- **UI resilience** — added a global safety net for background request failures, so a
  transient server/network hiccup during a poll or save no longer surfaces as an
  unhandled error or leaves the UI stuck. Saves and checkout keep their own feedback.

### Verified clean (no change needed)
The audit also confirmed the math/parsing layer is already well-defended — every average
and rate is divide-by-zero guarded, parsers degrade gracefully on bad input, atomic
writes clean up after themselves, and audio/file handles close on every path.

Update by downloading Fragnetic-Setup.zip below and replacing your existing folder
(your accounts, license, and history live outside the app folder and are untouched).
