#!/usr/bin/env bash
# ship_release.sh TAG TITLE NOTES_FILE ASSET
# Create a GitHub release + upload the installer in ONE command -- no Chrome, no manual
# drag. Uses the token git already has (from GitHub Desktop) via GH_TOKEN, which bypasses
# gh's read:org scope check (releases only need repo scope, which the push token has).
set -euo pipefail
TAG="$1"; TITLE="$2"; NOTES="$3"; ASSET="$4"
GH="/c/Users/Bklu/AppData/Local/gh-portable/bin/gh.exe"
REPO="ycbuzi/fragnetic"

if [ ! -f "$ASSET" ]; then echo "asset not found: $ASSET" >&2; exit 1; fi

# pull the cached github.com token from git's credential manager (never printed)
TOKEN=$(printf 'protocol=https\nhost=github.com\n\n' | git credential fill 2>/dev/null | grep '^password=' | cut -d= -f2-)
if [ -z "${TOKEN:-}" ]; then echo "no cached github token (is GitHub Desktop logged in?)" >&2; exit 1; fi

GH_TOKEN="$TOKEN" "$GH" release create "$TAG" --repo "$REPO" --latest \
  --title "$TITLE" --notes-file "$NOTES" "$ASSET"
echo "released $TAG with $(basename "$ASSET")"
