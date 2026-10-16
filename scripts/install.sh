#!/bin/bash
# Install Inkcast for the current user.
#
#   scripts/install.sh            whiteboard only
#   scripts/install.sh --mirror   also install UxPlay for AirPlay screen mirroring
#
# Linux (Omarchy/Arch): links `inkcast` into ~/.local/bin, adds launcher entries,
# and opens the needed firewall ports for private LANs only (ufw).
# macOS: links `inkcast` into ~/.local/bin and checks for poppler/qrencode.

set -e

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BIN_DIR="$HOME/.local/bin"
APP_DIR="$HOME/.local/share/applications"
ICON_DIR="$APP_DIR/icons"
PORT="${INKCAST_PORT:-8770}"
UFW_COMMENT="inkcast"
PRIVATE_CIDRS=(10.0.0.0/8 172.16.0.0/12 192.168.0.0/16)
# UxPlay with -p: TCP 7000 7001 7100, UDP 6000 6001 7011; mDNS on UDP 5353.
MIRROR_TCP=(7000 7001 7100)
MIRROR_UDP=(5353 6000 6001 7011)

WITH_MIRROR=false
for arg in "$@"; do
  case "$arg" in
  --mirror) WITH_MIRROR=true ;;
  -h | --help)
    sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'
    exit 0
    ;;
  *)
    echo "unknown option: $arg" >&2
    exit 1
    ;;
  esac
done

have() { command -v "$1" >/dev/null 2>&1; }

link_cli() {
  mkdir -p "$BIN_DIR"
  ln -sf "$ROOT/bin/inkcast" "$BIN_DIR/inkcast"
  echo "Linked $BIN_DIR/inkcast"
  case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) echo "Note: $BIN_DIR is not on your PATH." ;;
  esac
}

install_macos() {
  link_cli
  if ! have pdftoppm || ! have qrencode; then
    echo "For PDFs and the pairing QR code: brew install poppler qrencode"
  fi
  echo
  echo "Run: inkcast"
  echo "For iPad screen mirroring, macOS has a built-in AirPlay receiver:"
  echo "System Settings > General > AirDrop & Handoff > AirPlay Receiver."
}

install_packages() {
  local missing=()
  have pdftoppm || missing+=(poppler)
  have qrencode || missing+=(qrencode)
  if ((${#missing[@]})); then
    echo "Installing ${missing[*]}..."
    if have omarchy; then omarchy pkg add "${missing[@]}"; else sudo pacman -S --needed --noconfirm "${missing[@]}"; fi
  fi

  if $WITH_MIRROR && ! have uxplay; then
    echo "Installing UxPlay (AUR)..."
    if have omarchy; then
      omarchy pkg aur add uxplay
    elif have yay; then
      yay -S --needed uxplay
    else
      echo "Install the 'uxplay' package from the AUR, then re-run this script." >&2
      exit 1
    fi
  fi

  if $WITH_MIRROR && ! systemctl is-active --quiet avahi-daemon; then
    echo "Enabling avahi-daemon (lets the iPad discover this computer)..."
    sudo systemctl enable --now avahi-daemon
  fi
}

allow_port() {
  local proto="$1" port="$2" cidr
  for cidr in "${PRIVATE_CIDRS[@]}"; do
    sudo ufw allow in proto "$proto" from "$cidr" to any port "$port" comment "$UFW_COMMENT" >/dev/null
  done
}

open_firewall() {
  if ! have ufw; then
    return
  fi
  echo "Opening firewall ports for private LANs (needs sudo)..."
  allow_port tcp "$PORT"
  if $WITH_MIRROR; then
    local p
    for p in "${MIRROR_TCP[@]}"; do allow_port tcp "$p"; done
    for p in "${MIRROR_UDP[@]}"; do allow_port udp "$p"; done
  fi
  sudo ufw reload >/dev/null
}

write_desktop_entries() {
  mkdir -p "$APP_DIR" "$ICON_DIR"
  cp "$ROOT/web/icon.png" "$ICON_DIR/Inkcast.png"

  cat >"$APP_DIR/inkcast.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Inkcast
GenericName=Apple Pencil Whiteboard
Comment=Write on your iPad, show it live in a window on this computer
Exec=$BIN_DIR/inkcast board
Icon=$ICON_DIR/Inkcast.png
Terminal=false
Categories=Education;Office;Graphics;
Keywords=ipad;pencil;whiteboard;draw;notes;pdf;annotate;
StartupNotify=false
EOF

  if $WITH_MIRROR || have uxplay; then
    cat >"$APP_DIR/inkcast-mirror.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Inkcast Mirror
GenericName=AirPlay Receiver
Comment=Mirror your iPad or iPhone screen into a window (UxPlay)
Exec=$BIN_DIR/inkcast mirror
Icon=$ICON_DIR/Inkcast.png
Terminal=true
Categories=AudioVideo;Video;
Keywords=airplay;mirror;ipad;iphone;screen;uxplay;
EOF
  fi
  have update-desktop-database && update-desktop-database "$APP_DIR" >/dev/null 2>&1 || true
  echo "Added launcher entries (Super + Space > Inkcast)"
}

if [[ "$(uname)" == "Darwin" ]]; then
  install_macos
  exit 0
fi

link_cli
install_packages
open_firewall
write_desktop_entries

echo
echo "Done. Start the whiteboard with: inkcast"
$WITH_MIRROR && echo "Start AirPlay mirroring with:  inkcast mirror"
exit 0
