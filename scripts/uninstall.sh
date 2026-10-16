#!/bin/bash
# Remove what scripts/install.sh added. Your notebook in ~/.local/share/inkcast is kept.

set -e

"$HOME/.local/bin/inkcast" stop >/dev/null 2>&1 || true
rm -f "$HOME/.local/bin/inkcast"
rm -f "$HOME/.local/share/applications/inkcast.desktop" "$HOME/.local/share/applications/inkcast-mirror.desktop"
rm -f "$HOME/.local/share/applications/icons/Inkcast.png"

if command -v ufw >/dev/null 2>&1; then
  echo "Removing inkcast firewall rules (needs sudo)..."
  # Delete numbered rules tagged "inkcast", highest number first.
  sudo ufw status numbered | grep '# inkcast$' | sed -E 's/^\[ *([0-9]+)\].*/\1/' | sort -rn |
    while read -r n; do sudo ufw --force delete "$n" >/dev/null; done
  sudo ufw reload >/dev/null
fi

echo "Inkcast removed. (UxPlay, if installed, was left in place: omarchy pkg drop uxplay)"
