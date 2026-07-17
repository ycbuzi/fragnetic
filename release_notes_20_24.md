## Fragnetic 20.24 — privacy fix: no personal data in the download

**Important privacy fix.** A tester found that the downloaded app carried the owner's personal
data. Root cause: the build was bundling **runtime personal files** into the shipped exe/release —
the owner's **custom wallpaper** (inside `fragroute_icons.json`) and their **owned weapon-skin
collection** (`fragroute_weapon_skins.json`). Fixed so nothing personal can ship.

### Fixed
- The exe **no longer bundles** `fragroute_weapon_skins.json` (owned skins) or the raw runtime
  `fragroute_icons.json` (which held a custom wallpaper). A customer now starts with a clean slate.
- Instead the build bundles a **sanitized reference `icons.json`** (rank emblems, weapon-type
  glyphs, and the built-in wallpaper *presets* only) via a new `sanitize_ship_assets.py` that
  strips any personal slot — above all the custom `wallpaper`.
- `package_release.bat` now ships that sanitized file and its safety scan **aborts** if a bare
  `wallpaper` slot ever slips through.
- Regression guard added (`test_ship_privacy` in the smoke suite).

**No credentials were exposed** — account password hashes and license keys were never in the
release or exe (only the wallpaper + owned-skins). Still, the current download should be replaced.

Update by downloading the new Fragnetic-Setup.zip below.
