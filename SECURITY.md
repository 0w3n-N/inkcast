# Security

Inkcast runs a small web server on your computer so your iPad can reach it
over the local network. This page explains what that exposes, how it's
protected, and what the Omarchy plugin does on your system.

## What the plugin does on your system

- **Runs only when you start it.** Nothing autostarts. The bar widget runs
  `bin/inkcast` with argument lists, never through a shell, and reads status
  files from `$XDG_RUNTIME_DIR/inkcast/`.
- **Never uses sudo by itself.** Only `scripts/setup`, which you run in a
  terminal, uses sudo, and it asks before each step. It uses sudo for exactly:
  `pacman`/`yay` (poppler, qrencode, uxplay), `ufw allow` (firewall rules
  limited to private network ranges, tagged `inkcast`), and
  `systemctl enable avahi-daemon` (AirPlay discovery).
  `scripts/uninstall` reverses it.
- **Downloads nothing and runs no remote code.** No piping downloads into a shell, no bundled
  binaries, no pip packages. UxPlay comes from the AUR through your normal AUR
  helper, only if you choose it.
- **Changes no configuration without asking.** Setup asks before adding the
  optional `~/.local/bin/inkcast` link and launcher entries.
- **Stores data owner-only.** The notebook, PDFs and pairing key live in
  `~/.local/share/inkcast/` (mode 700; files 600).

## The whiteboard server

Listens on TCP 8770 on all interfaces (`inkcast serve --bind 127.0.0.1`
limits it to this computer).

- **Pairing key.** Every data endpoint and the WebSocket need a 128-bit key.
  It's created on first run and kept in `~/.local/share/inkcast/token`, and it
  travels inside the QR code. No request gets a localhost exemption, so other
  users on the same computer and web pages in your browser are locked out
  too. Delete the token file and restart to rotate it, which unpairs the iPad.
- **DNS rebinding.** The `Host` header must name this computer
  (`localhost`, `127.0.0.1` or one of its LAN addresses).
- **Cross-site requests.** WebSocket and POST requests that carry a browser
  `Origin` must come from Inkcast's own pages. Other websites can't connect
  to the server even from your desktop browser.
- **Opening the share window** uses a single-use code that expires after 60 s,
  so the long-lived key never appears on a process command line.
- **Input validation.** Every message from a device is rebuilt from validated
  fields before it is stored or relayed: IDs, colors (`#rrggbb`), numbers
  (finite and clamped), tools and paper types (allowlists). Uploads must start
  with `%PDF-`.
- **Limits.** Uploads are capped at 200 MB and WebSocket messages at 2 MB.
  Strokes, points, pages and connections are capped, idle connections time
  out, and PDF rendering runs under CPU and memory limits (`prlimit`). Only
  2 pages render at a time.
- **Pages.** A strict Content-Security-Policy (no inline scripts or styles,
  same-origin only), `nosniff`, `no-referrer`, and no framing. Static files
  come from a fixed allowlist, not the filesystem path.
- **Theme.** Only `#rrggbb` colors and a sanitized font name from your
  Omarchy theme reach the stylesheet.

**Not protected:** the connection is plain HTTP, because iPad Safari won't
trust a self-signed certificate without manual profile installation. On your
own Wi-Fi (WPA2/WPA3), traffic is encrypted between devices and the router.
On open or untrusted networks, someone on the same network could read your
strokes and the key. Don't run it there, or stop it when you're done.

## AirPlay mirroring

UxPlay is started with a random 4-digit PIN (`-pin`). Devices that entered it
are remembered in `~/.local/share/inkcast/airplay-devices`. Without the PIN,
someone else on the network could push their screen into your window.
`--no-pin` turns this off. UxPlay listens only while mirroring is on.

## Reporting a problem

Please open a GitHub issue. For anything sensitive, use a private security
advisory (Security tab › Report a vulnerability) instead of a public issue.
