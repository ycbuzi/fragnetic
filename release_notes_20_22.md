## Fragnetic 20.22 — VPN made optional & easy (region-lock marked experimental)

Feedback: needing a ProtonVPN WireGuard config felt like a barrier. It shouldn't be — a VPN
is optional. This release makes that clear and removes the friction for people who do want one.

### Changed
- **You don't need a VPN.** The routing tab now says so plainly: **region ping** works for
  everyone with zero setup, and a VPN only helps to *improve* ping to a **far** region.
- **Direct Region Lock is now labelled EXPERIMENTAL.** It works (firewall-based region switching,
  no VPN), but it's still being hardened — so it's honestly flagged, and the VPN route is the
  reliable option when you need a guaranteed switch.

### New
- **One-click config import.** A new **VPN Configs** panel on the routing tab: hit **Import .conf…**,
  pick your WireGuard file(s), and Fragnetic validates + files them for you and re-maps regions —
  no more hunting for the configs folder. (Open folder button still there for manual drops.)
- **"Get a free config" guide.** A built-in step-by-step for grabbing a free WireGuard config
  (ProtonVPN has a free tier; any provider works — Mullvad, IVPN, etc.). Your VPN credentials never
  leave your PC.

Backend: `POST /api/configs/import` (validates `[Interface]`/`[Peer]`, sanitizes filenames, rescans)
and `POST /api/configs/open`.

Update by downloading Fragnetic-Setup.zip below and replacing your existing folder.
