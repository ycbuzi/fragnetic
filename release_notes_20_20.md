## Fragnetic 20.20 — hide owner-only internals from customers

Tidies up what a normal customer sees. Some chrome was only ever meant for the
owner/admin build and shouldn't clutter a buyer's window.

### Changed
- **Build number is now owner-only.** The `· BUILD xx.xx` tag in the header is hidden
  for everyone except the admin/owner tier. Customers still get the in-app "a newer
  version is available" notice when an update ships — they just don't see the raw build
  churn on every screen.
- **Dev launch-flag readouts hidden.** *Settings → Data & Advanced* no longer shows the
  **Server port** and **Dry-run mode** rows to customers (they're launch-flag internals).
  Your **Configs folder** and **Reset all settings** stay visible.
- The admin-only **Label** (detector-training) tab remains owner-only, as before.

Mechanism: a single `admin-mode` body class, set from your entitlement tier, reveals
anything tagged `data-admin-only`. Everything stays hidden by default, so nothing
internal can leak to a buyer.

Update by downloading Fragnetic-Setup.zip below and replacing your existing folder.
