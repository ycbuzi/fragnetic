## Fragnetic 20.9 — deep-dive stability & hardening pass

This release is a broad reliability sweep. A full-app audit found and fixed 36 real
crash / data-loss / robustness issues across every subsystem. No new FPS cost — the
in-game footprint is unchanged.

### Match recording
- Recordings can no longer run past a match or span two matches — a stuck game-state
  detection is now hard-capped, and a mid-match crash/force-quit resets cleanly.
- Clip saves no longer fail if the rolling buffer rotates a segment mid-save (the file
  is skipped instead of failing the whole save).
- Recording now tracks a mid-match resolution / windowed change instead of clipping.

### VPN routing & region
- Fixed a rare crash when switching routes while a re-scan was running.
- A match joined in progress now detects its real mode instead of logging "unknown".
- Latency pings can no longer leak background threads or delay app exit.

### AI coach
- Text and vision models can no longer collide on the same port during startup.
- Cleaner model load/stop under rapid start/stop (no double-loaded server, no wasted GPU).

### Accounts, license & data safety
- Account and license writes are now crash-safe (no more loss on a hard kill).
- A revoked/expired key no longer burns one of your activations on every re-check.

### Setup / model downloads
- Truncated downloads are now detected and resumed instead of being accepted as complete.
- Tighter size + checksum verification so a partial model can't be treated as installed.

### Under the hood
- Dozens of smaller crash-hardening fixes: safer startup, no leaked timers in the UI,
  no unhandled errors when saving Locker edits, and more defensive state handling
  throughout.

Update by downloading Fragnetic-Setup.zip below and replacing your existing folder
(your accounts, license, and history live outside the app folder and are untouched).
