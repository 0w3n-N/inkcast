# Inkcast

<img src="assets/icon-256.png" alt="Inkcast icon" width="96" align="right">

Write on your iPad with the Apple Pencil and see it live in a window on your
Omarchy (or any Linux / macOS) desktop. Share that window in Zoom, Teams, Meet
or OBS. Or mirror the whole iPad screen over AirPlay.

Inkcast is in the spirit of [LocalSend](https://localsend.org): open source, no
accounts, no cloud, and no app to install on the iPad. Everything stays on your
local network.

![Inkcast preview](preview.png)

- **Whiteboard**: the iPad opens a page in Safari. The Pencil draws with
  pressure, palm touches are ignored, and the ink streams to a clean window on
  your computer. Write on blank, grid, lined or dotted paper, or on any PDF.
  In the window you can zoom with the mouse wheel and drag to move around.
- **Mirror**: full AirPlay screen mirroring into a window, powered by
  [UxPlay](https://github.com/FDH2/UxPlay), with a PIN so nobody else on the
  network can take over your screen. Use it with any iPad app.
- **Omarchy bar widget**: one icon to start it, a QR code to pair the iPad, and
  toggles for the share window and AirPlay. The icon and the window follow your
  Omarchy theme.

## Install (Omarchy plugin)

```bash
omarchy plugin add https://github.com/0w3n-N/inkcast.git --enable
```

Omarchy asks before cloning it (plugins run as your user, so it's worth a
look at the code first). With `--enable` the Inkcast icon then appears on the
right side of the bar. Then click it
and choose **Setup** (or run `scripts/setup` in the plugin folder). Setup asks
before each step:

1. install **poppler** (PDFs) and **qrencode** (the pairing QR code) if missing
2. optionally install **UxPlay** (AUR) and enable **avahi-daemon** for AirPlay
3. open the **firewall** (ufw) for private networks only, if ufw is active
4. optionally add an `inkcast` terminal command and launcher entries

Requirements: Python 3.9+ (already on Omarchy). No pip packages.

### Remove

```bash
~/.config/omarchy/plugins/io.github.0w3n-n.inkcast/scripts/uninstall   # undo setup (asks first)
omarchy plugin remove io.github.0w3n-n.inkcast
```

`scripts/uninstall` stops the server, removes the command and launcher
entries, and asks before removing the firewall rules or deleting your saved
notebook. Packages it installed (poppler, qrencode, UxPlay) are left in place.

### Other Linux and macOS

```bash
git clone https://github.com/0w3n-N/inkcast.git
inkcast/scripts/setup
inkcast/bin/inkcast
```

On macOS, install `poppler` and `qrencode` with Homebrew. For mirroring, use
the built-in AirPlay Receiver instead of UxPlay.

## Using the whiteboard

1. Click the Inkcast icon and switch it on (or right-click the icon to start it
   and open the share window in one go).
2. On the iPad, scan the QR code with the Camera app. Both devices need to be
   on the same Wi-Fi.
3. Optional: in Safari, tap Share › **Add to Home Screen** to get a full-screen app.
4. Write. In Zoom or Teams, share the window titled **Inkcast**.

On the iPad:

| Input | Action |
| --- | --- |
| Pencil | draw / highlight / erase, with pressure |
| Double-tap the Pencil tip on one spot | switch between pen and eraser (can be turned off in the ⋯ menu) |
| One finger | pan |
| Two fingers | pinch to zoom on the iPad only |
| ⋯ menu | open a PDF, change paper, clear or delete a page, new notebook, save as PDF, draw with a finger |

In the share window (move the mouse to show the controls; they hide again so
your screen share stays clean):

| Input | Action |
| --- | --- |
| Mouse wheel / touchpad pinch | zoom at the pointer |
| Drag, or two-finger scroll | move around |
| Double-click / `0` | zoom 2× / back to fit |
| `+` `-` | zoom in / out |
| `L` | follow the pen while zoomed in (on by default) |
| `←` `→` / Space | previous / next page (the iPad follows) |
| `Q` | show or hide the pairing QR code |
| `O` | open a PDF (or drop one onto the window) |
| `S` | save the whole notebook, ink included, as a PDF |
| `F` | full screen |

The Pencil's barrel double-tap and the Pencil Pro squeeze only reach native
apps; Safari doesn't pass them to web pages. That's why Inkcast uses a quick
double-tap of the tip instead. The taps must land within about 16 px and
300 ms of each other, so a colon or an ellipsis won't trigger it.

On iPads with Pencil hover (M2 iPad Pro and newer), a red dot follows the
hovering Pencil, so people on the call can see where you're pointing.

The notebook autosaves to `~/.local/share/inkcast/` and survives restarts.

## Mirroring (AirPlay)

Click the Inkcast icon › **AirPlay receiver** (or middle-click the icon). On
the iPad: Control Center › Screen Mirroring › **Omarchy**, then enter the
4-digit PIN shown in the Inkcast popup. Devices that entered the PIN once are
remembered. The iPad screen opens in a window you can share like any other.

**Cabled mirroring isn't supported.** Apple doesn't expose iPad screen capture
over USB on Linux, and the open source attempts rely on reverse-engineered
protocols that break between iOS versions. For the lowest latency, use 5 GHz
Wi-Fi or have the computer on Ethernet.

## Bar widget

| Click | Action |
| --- | --- |
| Left | open the popup |
| Right | start the whiteboard and open the share window |
| Middle | toggle AirPlay mirroring |

Icon color: dim when off, normal while the whiteboard waits for an iPad, and
your theme's accent color while an iPad is connected or mirroring is on.

Settings live on the widget's entry in `~/.config/omarchy/shell.json`:
`port` (default 8770), `airplayName` (default `Omarchy`), `airplayAudio`
(`on`/`off`).

Bind keys with the IPC target, e.g. in `~/.config/hypr/bindings.lua`:
`omarchy-shell io.github.0w3n-n.inkcast board` (open the share window),
`... toggle` (popup), `... mirror`, `... start`, `... stop`.

## Command line

```
inkcast              start if needed and open the share window
inkcast start        start the server in the background
inkcast open         open the share window
inkcast url          print the iPad link and QR code in the terminal
inkcast status       running / connected devices (--json)
inkcast doctor       check dependencies, firewall and network (--json)
inkcast stop         stop the server
inkcast serve        run the server in the foreground
inkcast mirror       AirPlay receiver; --pin NNNN, --no-pin, --no-audio, --fullscreen, --name NAME
```

`--port` changes the port (default 8770 or `$INKCAST_PORT`). `--host` sets the
address shown to the iPad if auto-detection picks the wrong one; it already
skips VPN interfaces (ProtonVPN, WireGuard, Tailscale).

## Security

Short version: only devices that scanned your QR code can see or change your
notebook, the server only runs when you start it, and nothing leaves your
network. Details, including what the plugin can and can't do on your system,
are in [SECURITY.md](SECURITY.md).

## How it works

- `inkcast/server.py`: a dependency-free Python server (standard library only)
  that serves the pages, relays strokes over WebSockets, validates every
  message, and renders PDF pages with `pdftoppm`.
- `web/`: the iPad pad and the share window. Both use `web/ink.js` to draw.
- `Panel.qml`, `InkcastIcon.qml`, `Model.js`: the Omarchy bar widget. It runs
  `bin/inkcast` with argument lists (never a shell) and reads one status file.
- `assets/build-icons.py`: regenerates the icon from its geometry (needs Inkscape).

## License

MIT. The icon is original artwork; the apple is a generic fruit, not Apple's logo.
