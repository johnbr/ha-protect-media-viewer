#!/usr/bin/env bash
# Symlink (or copy) the integration into a Home Assistant config dir for testing.
#
# Usage:
#   scripts/deploy.sh /path/to/homeassistant/config            # symlink (default)
#   scripts/deploy.sh /path/to/homeassistant/config --copy     # copy instead
#
# After deploying, restart Home Assistant and add the integration via
# Settings -> Devices & Services -> Add Integration -> "Protect Media Viewer".
set -euo pipefail

CONFIG_DIR="${1:-}"
MODE="${2:---link}"

if [[ -z "$CONFIG_DIR" ]]; then
  echo "Usage: $0 /path/to/homeassistant/config [--link|--copy]" >&2
  exit 1
fi

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="$REPO_DIR/custom_components/protect_media_viewer"
DEST_PARENT="$CONFIG_DIR/custom_components"
DEST="$DEST_PARENT/protect_media_viewer"

mkdir -p "$DEST_PARENT"
rm -rf "$DEST"

if [[ "$MODE" == "--copy" ]]; then
  cp -r "$SRC" "$DEST"
  echo "Copied integration to $DEST"
else
  ln -s "$SRC" "$DEST"
  echo "Symlinked $DEST -> $SRC"
fi

echo "Now restart Home Assistant and add the integration."
