# Security

Inkcast runs a web server on your computer so the iPad can reach it over the
local network. This file covers what that exposes and what the plugin does on
your system.

## What runs, and with which privileges

- Nothing starts automatically. The server runs only while you have it
  switched on.
- The bar widget runs `bin/inkcast` with argument lists, never a shell string,
  and reads status files in `$XDG_RUNTIME_DIR/inkcast/`. It never uses sudo.
- `scripts/setup` (run by you, in a terminal) asks before each step. It uses
  sudo only for `pacman`/`yay` package installs, `ufw allow` rules limited to
  private network ranges and tagged `inkcast`, and
  `systemctl enable --now avahi-daemon`. `scripts/uninstall` removes the rules
  and the optional command and launcher entries.
- No code is downloaded at runtime, and there are no bundled binaries or pip
  packages.
- Data (notebook, PDFs, frozen window frames, pairing key) is stored in
  `~/.local/share/inkcast/`. The directory is mode 700 and the files are 600.

## Whiteboard server (TCP 8770)

- **Pairing key.** Every data request and WebSocket needs a 128-bit key from
  `~/.local/share/inkcast/token`, which is carried in the QR code. There's no
  exception for localhost, so other local users and websites open in your
  browser are refused. Delete the file and restart to issue a new key; this
  unpairs every iPad.
- **Browser attacks.** Requests whose `Host` isn't this computer are refused,
  which blocks DNS rebinding. WebSocket and POST requests that carry an
  `Origin` must come from Inkcast's own pages, which blocks cross-site use.
- **Opening the share window** uses a one-time code that expires after 60 s,
  so the key never appears on a process command line.
- **Input** from devices is validated and rebuilt before it's stored or
  relayed. This covers IDs, colors, finite and clamped numbers, and
  allowlisted tools and paper types. Uploads must be PDFs.
- **Limits:** 200 MB per upload, 2 MB per message, caps on strokes, points,
  pages and connections, and socket timeouts. PDF rendering runs under CPU and
  memory limits (`prlimit`).
- **Pages** are served with a strict Content-Security-Policy, `nosniff`,
  `no-referrer` and no framing. Static files come from a fixed allowlist.
- **Not protected:** traffic is plain HTTP, because iPad Safari won't trust a
  self-signed certificate. On your own WPA2/WPA3 Wi-Fi, the radio link is
  encrypted. On open or shared networks, others could read your strokes and
  the key, so don't use it there.

## Desktop annotation

- Window capture goes through the standard screen-share portal. A window is
  only captured after someone at the computer picks it in the share picker. A
  paired iPad can ask for the picker to open, but can't choose a window
  itself.
- Frames go only to clients that hold the key. Capture stops when you stop it,
  when you delete its page, or when the window closes. The last frame is saved
  as an owner-only file. Starting a new notebook deletes the saved frames.

## AirPlay mirroring

UxPlay starts with a random 4-digit PIN, and devices that entered it are
remembered in `~/.local/share/inkcast/airplay-devices`. UxPlay only listens
while mirroring is on. `--no-pin` turns the PIN off.

## Reporting a problem

For a security problem, open an issue asking for a private contact. Don't
post the details publicly. Use issues as normal for everything else.
