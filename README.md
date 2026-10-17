# Inkcast

<img src="assets/icon-256.png" alt="" width="88" align="right">

Use an iPad and Apple Pencil as a drawing tablet for your Omarchy desktop.
You write in Safari on the iPad, and the ink appears live in a window on your
computer that you can share in Zoom, Teams or Meet. You can write on blank
paper, a PDF, or a live view of any desktop window. Inkcast can also mirror
the whole iPad screen over AirPlay.

It's an Omarchy bar plugin with a small Python server. The server runs on your
computer only while you have it switched on. Nothing is installed on the iPad,
and no data leaves your local network.

![Inkcast preview](preview.png)

## Requirements

- Omarchy, for the bar plugin. The command line also works on other Linux
  desktops and on macOS.
- An iPad with Safari, on the same Wi-Fi network as the computer.
- Python 3.9+. No pip packages.
- Optional:

| Package | Needed for |
| --- | --- |
| `poppler`, `qrencode` | writing on PDFs; the pairing QR code |
| `python-gobject`, `gst-plugin-pipewire`, `gst-plugins-good` | desktop annotation (Linux; usually already on Omarchy) |
| `uxplay` (AUR), `avahi-daemon` | AirPlay mirroring |

`scripts/setup` installs these, and asks first.

## Install

```bash
omarchy plugin add https://github.com/0w3n-N/inkcast.git --enable
```

Then click the Inkcast icon in the bar and choose **Setup**. It runs
`scripts/setup` in a terminal, and every step asks before changing anything:

1. Install the missing packages from the table above.
2. Open the firewall (ufw) for the whiteboard (TCP 8770) and, if UxPlay is
   installed, the AirPlay ports. The rules only allow private network ranges.
3. Optional: an `inkcast` terminal command and app-launcher entries.

To remove it:

```bash
~/.config/omarchy/plugins/io.github.0w3n-n.inkcast/scripts/uninstall
omarchy plugin remove io.github.0w3n-n.inkcast
```

`scripts/uninstall` asks before it removes the firewall rules or deletes your
notebook. It leaves installed packages in place.

**Without Omarchy:** `git clone` the repo, run `scripts/setup`, then
`bin/inkcast`.

## Use

### Pair the iPad

1. Click the bar icon and switch Inkcast on.
2. Scan the QR code in the popup with the iPad Camera app. The page opens in
   Safari.
3. Optional: Share › **Add to Home Screen** for a full-screen app. The link
   includes the pairing key, so keep it private.

### Share your writing

Click **Open the share window** (or right-click the bar icon). In Zoom or
Teams, share the window titled **Inkcast**. Its controls hide when the mouse
is still, so they don't show up on the call.

### Write on a PDF

On the iPad, choose ⋯ › **Open PDF**, or drop a PDF onto the share window.
Each PDF page becomes a page you can write on. Save the whole notebook,
ink included, with ⋯ › **Save notebook as PDF** or `S` in the share window.

### Annotate a desktop window

Show one desktop window (slides, code, a simulation, a web page) on the iPad
and draw over it:

1. Click **Annotate a window** in the bar popup (or ⋯ on the iPad, or
   **Window** in the share window).
2. Pick the window in Omarchy's share picker on the computer. Someone at the
   iPad can't skip this step.
3. The window appears on the iPad as a new page, updating live at up to 15
   frames per second. Draw on it with the normal tools. Pinch-zoom on the iPad
   to look closer; this doesn't change the window or the share window.
4. Stop it from the same places. The last frame stays on the page, so your
   notes keep their context.

The ink is placed on the page, not tied to the window's content, so it
doesn't follow when the window scrolls. Clear the page (⋯ › Clear this page)
or start a new window page when the content moves. Don't pick the Inkcast
share window itself, or you'll get a picture of a picture.

### Mirror the iPad (AirPlay)

Toggle **AirPlay receiver** in the popup (or middle-click the icon). On the
iPad: Control Center › Screen Mirroring › **Omarchy**, then enter the 4-digit
PIN shown in the popup. Devices are remembered after the first PIN. This works
with any iPad app, but there's no cabled option.

## Controls

**iPad**

| Input | Action |
| --- | --- |
| Pencil | draw, highlight or erase, with pressure |
| Pencil tip double-tap on one spot | switch between pen and eraser (turn off in ⋯) |
| One finger | pan |
| Two fingers | zoom (iPad only) |
| ⋯ | PDFs, desktop window, paper style, clear/delete page, new notebook, save as PDF, finger drawing |

The barrel double-tap that works in native apps (like OneNote) isn't
available here: iPadOS only delivers it to native apps, and Safari doesn't
pass it to web pages. A quick double-tap of the tip on the screen does the same
job, and the two dots it leaves are removed.

**Share window**

| Input | Action |
| --- | --- |
| Mouse wheel / pinch | zoom at the pointer |
| Drag / two-finger scroll | pan |
| Double-click, `0` | zoom 2×, back to fit |
| `+` `-` | zoom in, out |
| `L` | follow the pen while zoomed (on by default) |
| `←` `→` Space | change page (the iPad follows) |
| `W` | annotate a desktop window / stop |
| `O` `S` `Q` `F` | open PDF, save PDF, pairing QR, full screen |

**Bar icon:** left-click opens the popup, right-click opens the share window,
middle-click toggles AirPlay. The icon is dimmed when off, normal while waiting
for the iPad, and accent-colored while the iPad is connected, a window is live,
or mirroring is on.

Keyboard shortcuts can call the widget directly, e.g.
`omarchy-shell io.github.0w3n-n.inkcast board` (share window). The other
actions are `toggle`, `annotate`, `mirror`, `start` and `stop`.

Settings live on the widget's entry in `~/.config/omarchy/shell.json`: `port`
(8770), `airplayName` (`Omarchy`), `airplayAudio` (`on`/`off`).

## Command line

```
inkcast              start if needed and open the share window
inkcast annotate     pick a desktop window to annotate (--stop to end it)
inkcast url          print the iPad link and QR code
inkcast status       running, connected devices (--json)
inkcast doctor       check packages, firewall and network
inkcast start|stop   start or stop the server
inkcast mirror       AirPlay receiver (--pin, --no-pin, --no-audio, --name)
```

`--port` sets the port. `--host` sets the address shown to the iPad if the
wrong one is picked; VPN interfaces are skipped automatically.

## Troubleshooting

- **The iPad can't open the page.** Run `inkcast doctor`. Usually ufw is
  blocking the port (run Setup), the iPad is on a different network (guest
  Wi-Fi often isolates devices), or a VPN on the computer blocks LAN traffic.
  With ProtonVPN, allow LAN connections in its settings.
- **"This link is missing its key".** The key changed. Scan the QR code again.
- **No picker appears when annotating.** Check that xdg-desktop-portal-hyprland
  is running: `systemctl --user status xdg-desktop-portal-hyprland`.
- **AirPlay doesn't list Omarchy.** avahi-daemon must be running, and the
  AirPlay ports must be open (Setup does both).

Logs: `~/.local/state/inkcast/server.log`. Data: `~/.local/share/inkcast/`.

## Security

Only devices that scanned your QR code can see or change the notebook. The
connection is plain HTTP, so use it on networks you trust. See
[SECURITY.md](SECURITY.md) for details.

## Development

- `python3 -B tests/run.py` runs the security, protocol and annotation tests.
  They need poppler and GStreamer.
- `python3 assets/build-icons.py` regenerates the icons. It needs Inkscape.
- Layout: `inkcast/` (server, CLI, window capture, theme), `web/` (iPad page and
  share window), `Panel.qml` + `InkcastIcon.qml` + `Model.js` (bar widget).

## License

MIT. The icon is original artwork; the apple is a generic fruit, not Apple's logo.
