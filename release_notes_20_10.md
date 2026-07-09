## Fragnetic 20.10 — recurring-crash fix (firewall / region lock)

A runtime diagnostics review found a recurring background crash that the static audit
couldn't catch.

### Fixed
- **Region lock reliability**: the Windows firewall (`netsh`) output on many systems
  contains characters the default text decoder can't read. Every firewall reconcile was
  crashing a background reader thread and losing the command output — so region-lock rules
  could apply or clear without being verified, and the diagnostics log filled with errors.
  All external-tool output (firewall, GPU probe, process list, image/video tools) now
  decodes safely, so this class of crash can't happen again.
- Added a build-time regression guard so this decoding bug can never be reintroduced.

No FPS impact. If you're on 20.9, this is a small but worthwhile reliability update —
region lock now confirms every rule it sets.

Update by downloading Fragnetic-Setup.zip below and replacing your existing folder
(your accounts, license, and history live outside the app folder and are untouched).
