# Inkcast

Write on your iPad with the Apple Pencil and see it live in a window on your
Linux or Mac desktop. Share that window in Zoom, Teams, Meet or OBS.

Inkcast is in the same spirit as [LocalSend](https://localsend.org): open
source, no accounts, no cloud, no app to install on the iPad. Everything stays
on your local network.

- **Whiteboard mode** (`inkcast`): the iPad opens a web page in Safari. The
  Pencil draws with pressure; palm touches are ignored. Ink streams to a
  clean, chrome-less window on the computer. Write on a blank, grid, lined or
  dotted page, or on top of any PDF.
- **Mirror mode** (`inkcast mirror`): full AirPlay screen mirroring into a
  window, powered by [UxPlay](https://github.com/FDH2/UxPlay). Use it with
  any iPad app, such as GoodNotes, Notability or Freeform.

## Install

Requirements: Python 3.9+, poppler (`pdftoppm`, `pdfinfo`) for PDFs, and
`qrencode` for the pairing QR code (optional). No pip packages are needed.

```bash
git clone https://github.com/0w3n-N/inkcast.git ~/Projects/inkcast
cd ~/Projects/inkcast
scripts/install.sh            # whiteboard only
scripts/install.sh --mirror   # also installs UxPlay for AirPlay mirroring
```

On **Omarchy/Arch**, the installer:

- links `inkcast` into `~/.local/bin`
- installs poppler/qrencode if missing (and UxPlay from the AUR with `--mirror`)
- opens the needed ports in `ufw` for private LAN ranges only (10/8, 172.16/12, 192.168/16)
- adds **Inkcast** and **Inkcast Mirror** to the app launcher (Super + Space)

On **macOS**, it links the command; then run `brew install poppler qrencode`.
For mirroring, use macOS's built-in AirPlay Receiver instead of UxPlay.

`scripts/uninstall.sh` removes everything the installer added. Your notebook is kept.

## Whiteboard

1. Run `inkcast` (or launch **Inkcast** from the app launcher). A viewer window
   opens with a QR code.
2. On the iPad, scan the QR code with the Camera app, or type the link into Safari.
   Both devices must be on the same Wi-Fi.
3. Optional: in Safari, tap Share › **Add to Home Screen** for a full-screen app.
4. Write. In Zoom or Teams, share the window titled **Inkcast**.

On the iPad:

| Input | Action |
| --- | --- |
| Pencil | draw / highlight / erase |
| Double-tap the Pencil tip (same spot, quickly) | switch between pen and eraser (can be turned off in the ⋯ menu) |
| One finger | pan |
| Two fingers | pinch to zoom (only on the iPad; the shared window always shows the full page) |
| Menu › Draw with finger too | draw with a finger (no Pencil) |
| ⋯ menu | open PDF, change paper, clear page, delete page, new notebook, save as PDF |

In the viewer window (move the mouse to show the controls):

| Key | Action |
| --- | --- |
| `←` `→` / Space | previous / next page (the iPad follows) |
| `Q` | show or hide the pairing QR code |
| `O` | open a PDF (or drag and drop a PDF onto the window) |
| `S` | save the whole notebook, ink included, as a PDF |
| `F` | full screen |

The Pencil's own barrel double-tap and the Pencil Pro squeeze only reach
native apps; Safari doesn't pass them to web pages. Inkcast uses a quick
double-tap of the tip on the screen instead. The two taps must land within
about 16 px and 300 ms of each other, so a colon or an ellipsis won't trigger
it, and the dots the taps leave are removed automatically.

On iPads that support hover (M2 iPad Pro and newer), a red dot follows the
hovering Pencil, so people on the call can see where you're pointing.

Your notebook autosaves to `~/.local/share/inkcast/` and survives restarts.

### Commands

```
inkcast              start (if needed) and open the viewer window
inkcast url          print the iPad link and QR code in the terminal
inkcast stop         stop the background server
inkcast serve        run the server in the foreground (logs to the terminal)
inkcast mirror       AirPlay receiver (UxPlay); --no-audio, --fullscreen, --name NAME
```

`--port` changes the port (default 8770, or `$INKCAST_PORT`). `--host` sets
the address advertised to the iPad if auto-detection picks the wrong one.
Auto-detection skips VPN interfaces like ProtonVPN, WireGuard and Tailscale.

## Mirror mode (AirPlay)

```bash
inkcast mirror
```

On the iPad: Control Center › Screen Mirroring › **Omarchy**. The iPad's screen
appears in a window, which you can screen-share like any other window.
Ctrl+C stops the receiver.

This needs `avahi-daemon` running (the installer enables it) and the firewall
ports the installer opens: TCP 7000, 7001, 7100 and UDP 5353, 6000, 6001, 7011.

**Cabled mirroring** is not supported. Apple doesn't expose iPad screen capture
over USB on Linux. The only open source attempts rely on reverse-engineered
QuickTime protocols that break between iOS versions. AirPlay over Wi-Fi is
the reliable open route. For the lowest latency, use 5 GHz Wi-Fi or have the
computer on Ethernet.

## Security

Whiteboard mode serves plain HTTP on your LAN. iPad access requires a random
key, which is embedded in the QR code and link and stored in
`~/.local/share/inkcast/token`. Delete that file and restart to rotate the key.
The viewer and the admin endpoints only answer to `localhost`. Firewall rules
from the installer accept connections from private address ranges only.

## How it works

`inkcast/server.py` is a single, dependency-free Python server (standard
library only). It serves the pages, relays strokes over WebSockets, and
rasterizes PDF pages on demand with `pdftoppm`. The iPad and the viewer share
`web/ink.js` for rendering. Pages use a 1000-unit-wide coordinate space, so
the ink lines up at any window size. PDF export happens in the browser with a
tiny built-in PDF writer.

## License

MIT
