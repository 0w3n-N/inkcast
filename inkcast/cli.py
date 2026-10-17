"""inkcast command line."""

from __future__ import annotations

import argparse
import hmac
import ipaddress
import json
import os
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import __version__
from .server import data_dir, ping_proof, private_dir, runtime_dir, write_private

IS_MAC = sys.platform == "darwin"
IS_WINDOWS = os.name == "nt"


def default_port():
    try:
        port = int(os.environ.get("INKCAST_PORT", "8770"))
        return port if 1024 <= port <= 65535 else 8770
    except ValueError:
        return 8770


def port_arg(value):
    port = int(value)
    if not 1024 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1024 and 65535")
    return port


def host_arg(value):
    """An IP address or a plain hostname: it ends up in URLs and Host checks."""
    try:
        ipaddress.ip_address(value)
        return value
    except ValueError:
        pass
    if re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?", value):
        return value.lower()
    raise argparse.ArgumentTypeError("expected an IP address or hostname")


def log_dir() -> Path:
    if IS_WINDOWS:
        return private_dir(data_dir() / "logs")
    if IS_MAC:
        return private_dir(Path(os.path.expanduser("~/Library/Logs/inkcast")))
    base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return private_dir(Path(base) / "inkcast")


def local(port, path):
    return f"http://127.0.0.1:{port}{path}"


def token():
    try:
        return (data_dir() / "token").read_text().strip()
    except OSError:
        return ""


def probe(port):
    """Who is listening on the port: "ours", "other", or None (nothing).

    Any user on this computer can listen on a free port, so the server has to
    prove it knows our key (an HMAC of a fresh challenge) before the CLI sends
    the key to it.
    """
    nonce = secrets.token_hex(16)
    try:
        with urllib.request.urlopen(local(port, f"/api/ping?n={nonce}"), timeout=2) as r:
            data = json.load(r)
    except urllib.error.URLError as e:
        if isinstance(e.reason, ConnectionRefusedError):
            return None
        return "other" if not isinstance(e.reason, (TimeoutError, socket.timeout)) else None
    except (OSError, ValueError):
        return "other"
    key = token()
    if key and isinstance(data, dict) and isinstance(data.get("proof"), str) \
            and hmac.compare_digest(data["proof"], ping_proof(key, nonce)):
        return "ours"
    return "other"


def ping(port):
    return probe(port) == "ours"


def not_ours(port):
    return (f"inkcast: something else is using port {port}, not your Inkcast server, so it won't be "
            f"sent your key. Stop that program, or use another port (--port or INKCAST_PORT). "
            f"If it's an older Inkcast still running, stop it with: pkill -f 'inkcast serve'")


def request(port, path, method="GET", timeout=3, body=None):
    if probe(port) != "ours":
        sys.exit(not_ours(port))
    headers = {"X-Inkcast-Key": token()}
    if body is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(local(port, path), method=method, headers=headers,
                                 data=json.dumps(body).encode() if body is not None else None)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def print_pad_urls(port):
    urls = request(port, "/api/info")["urls"]
    if not urls:
        print("No Wi-Fi/Ethernet address found. Connect to a network, or pass --host <ip>.")
        return
    print("On your iPad, scan this or open the link in Safari (same Wi-Fi):\n")
    for u in urls:
        print(f"  {u}")
    print(flush=True)
    print_qr(urls[0])


def print_qr(url):
    """Print the pairing QR code in the terminal (made in-process, see qr.py)."""
    from . import qr

    try:
        sys.stdout.write(qr.terminal(url))
    except UnicodeEncodeError:  # a terminal that can't show block characters
        print("(QR code not shown: this terminal can't display it)")
    sys.stdout.flush()


def windows_browsers():
    """Chromium-based browsers on Windows (Edge ships with Windows 11)."""
    roots = [os.environ.get(v) for v in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA")]
    rel = (r"Google\Chrome\Application\chrome.exe", r"Microsoft\Edge\Application\msedge.exe",
           r"BraveSoftware\Brave-Browser\Application\brave.exe", r"Chromium\Application\chrome.exe")
    return [str(Path(r) / p) for p in rel for r in roots if r and (Path(r) / p).is_file()]


def open_window(url):
    """Open the viewer as a chrome-less app window (screen-share friendly)."""
    if IS_WINDOWS:
        for exe in windows_browsers():
            subprocess.Popen([exe, f"--app={url}"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             creationflags=0x00000008)  # DETACHED_PROCESS
            return
        os.startfile(url)  # default browser, as a normal tab
        return
    quiet = {"start_new_session": True, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if shutil.which("omarchy-launch-webapp"):
        subprocess.Popen(["omarchy-launch-webapp", url], **quiet)
        return
    for b in ("chromium", "google-chrome-stable", "google-chrome", "brave", "microsoft-edge-stable"):
        if shutil.which(b):
            subprocess.Popen([b, f"--app={url}"], **quiet)
            return
    if IS_MAC:
        for app in ("Google Chrome", "Chromium", "Brave Browser", "Microsoft Edge"):
            if Path(f"/Applications/{app}.app").exists():
                subprocess.Popen(["open", "-na", app, "--args", f"--app={url}"])
                return
    import webbrowser
    webbrowser.open(url)


def start_background(args):
    state = probe(args.port)
    if state == "ours":
        return True
    if state == "other":
        print(not_ours(args.port), file=sys.stderr)
        return False
    log = log_dir() / "server.log"
    cmd = [sys.executable, "-B", "-m", "inkcast", "serve", "--port", str(args.port), "--quiet"]
    if args.host:
        cmd += ["--host", args.host]
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parent.parent)}
    # Detach so the server outlives this command (and, on Windows, the console).
    detach = {"creationflags": 0x08000000 | 0x00000200} if IS_WINDOWS else {"start_new_session": True}
    fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "ab") as f:
        subprocess.Popen(cmd, stdout=f, stderr=f, stdin=subprocess.DEVNULL, env=env, **detach)
    for _ in range(80):
        if ping(args.port):
            return True
        time.sleep(0.1)
    print(f"inkcast: the server did not start, see {log}", file=sys.stderr)
    return False


# --------------------------------------------------------------------------- commands


def cmd_serve(args):
    from .server import InkcastServer

    try:
        srv = InkcastServer(args.bind, args.port, args.host)
    except OSError as e:
        sys.exit(f"inkcast: cannot listen on port {args.port}: {e}")

    def stop(*_):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    if not args.quiet:
        print(f"Inkcast {__version__} listening on port {args.port}  (Ctrl+C to stop)")
        for u in srv.pad_urls():
            print(f"iPad: {u}")
        sys.stdout.flush()
        if srv.pad_urls():
            print_qr(srv.pad_urls()[0])
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


def cmd_start(args):
    if not start_background(args):
        sys.exit(1)


def cmd_open(args, extra=""):
    if not start_background(args):
        sys.exit(1)
    code = request(args.port, "/api/launch", "POST")["c"]
    # The browser's command line is visible to other users, so it only gets the
    # path of an owner-only page; that page passes the one-time code on in the
    # URL fragment (#c=...), which never reaches a command line or server log.
    # The server deletes the page once the code is used or expires.
    from .server import launcher_path, write_private
    target = local(args.port, f"/view#c={code}{extra}")
    page = launcher_path(code)
    write_private(page, '<!doctype html><meta charset="utf-8"><title>Inkcast</title>'
                        f'<meta http-equiv="refresh" content="0;url={target}">')
    open_window(page.as_uri())


def cmd_annotate(args):
    """Desktop annotation: pick a window to show on the iPad and draw on."""
    if args.stop:
        if ping(args.port):
            request(args.port, "/api/live", "POST", timeout=5, body={"action": "stop"})
        return
    from .capture import missing_dependencies

    if not sys.platform.startswith("linux") or missing_dependencies():
        # Windows/macOS (or Linux without the GStreamer stack): the share window
        # captures the window through the browser, which needs a click there.
        cmd_open(args, "&annotate=1")
        if sys.stdout.isatty():
            print('In the Inkcast window, click "Choose a window" and pick the window to show on the iPad.')
        return
    if not start_background(args):
        sys.exit(1)
    request(args.port, "/api/live", "POST", timeout=10, body={"action": "start"})
    if sys.stdout.isatty():
        print("Pick the window in the share dialog. It appears on the iPad as a new page.")


def cmd_board(args):
    cmd_open(args)
    if sys.stdout.isatty():
        print_pad_urls(args.port)


def cmd_stop(args):
    if not ping(args.port):
        print("inkcast is not running")
        return
    try:
        request(args.port, "/api/shutdown", "POST")
    except urllib.error.HTTPError as e:
        sys.exit(f"inkcast: could not stop the server ({e.code})")
    for _ in range(30):
        if not ping(args.port):
            break
        time.sleep(0.1)
    print("inkcast stopped")


def cmd_url(args):
    if not ping(args.port):
        sys.exit("inkcast is not running (start it with: inkcast)")
    print_pad_urls(args.port)


def cmd_status(args):
    try:
        status = json.loads((runtime_dir() / "status.json").read_text())
    except (OSError, ValueError):
        status = {"running": False}
    status["running"] = ping(args.port)
    if args.json:
        print(json.dumps(status))
    elif status["running"]:
        print(f"running · {status.get('pads', 0)} iPad(s) · {status.get('views', 0)} viewer window(s)")
        print(status.get("url", ""))
    else:
        print("stopped")


def cmd_mirror(args):
    """AirPlay screen mirroring via UxPlay."""
    if IS_MAC:
        sys.exit("macOS has a built-in AirPlay receiver: System Settings > General > AirDrop & Handoff > AirPlay Receiver.")
    if IS_WINDOWS:
        sys.exit("AirPlay mirroring isn't supported on Windows. The whiteboard, PDFs and window annotation all work.")
    if not shutil.which("uxplay"):
        sys.exit("UxPlay is not installed. Run the setup: scripts/setup  (or: omarchy pkg aur add uxplay)")
    if shutil.which("systemctl") and subprocess.run(["systemctl", "is-active", "--quiet", "avahi-daemon"]).returncode != 0:
        print("warning: avahi-daemon is not running, so the iPad won't find this computer.\n"
              "         Run scripts/setup to enable it.", file=sys.stderr)
    # -p: fixed ports (TCP 7000 7001 7100, UDP 6000 6001 7011) so firewall rules can match.
    # -nh: no "@hostname" suffix on the AirPlay name.
    cmd = ["uxplay", "-n", args.name, "-nh", "-p"]
    if not args.no_pin:
        # A PIN stops anyone else on the network from taking over your screen.
        # It goes in an owner-only options file (-rc), not on the command line,
        # which other users can read. -rc replaces ~/.uxplayrc, so copy that in.
        # "reg" remembers devices that entered the PIN, so they aren't asked again.
        pin = args.pin or f"{secrets.randbelow(10000):04d}"
        lines = user_uxplayrc() + [f"pin {pin}", f"reg {data_dir() / 'airplay-devices'}"]
        rc = runtime_dir() / "uxplayrc"
        write_private(rc, "\n".join(lines) + "\n")
        cmd += ["-rc", str(rc)]
    # Let the bar plugin show the name and PIN (owner-only file).
    write_private(runtime_dir() / "mirror.json",
                  json.dumps({"name": args.name, "pin": "" if args.no_pin else pin, "pid": os.getpid()}))
    if args.fullscreen:
        cmd.append("-fs")
    if args.no_audio:
        cmd += ["-as", "0"]
    cmd += args.uxplay_args
    print(f'AirPlay receiver "{args.name}" is running. Ctrl+C to stop.')
    print(f"On the iPad: Control Center > Screen Mirroring > {args.name}")
    if not args.no_pin:
        print(f"PIN (first time only): {pin}")
    sys.stdout.flush()
    os.execvp(cmd[0], cmd)


def cmd_doctor(args):
    """What's installed and configured; the bar plugin reads --json."""
    from .server import lan_addresses

    def active(unit):
        if not shutil.which("systemctl"):
            return None
        return subprocess.run(["systemctl", "is-active", "--quiet", unit]).returncode == 0

    ufw_active = active("ufw") if shutil.which("ufw") else False
    marker = data_dir() / "firewall"
    if IS_WINDOWS:
        firewall = "configured" if marker.exists() else "windows"
    elif IS_MAC:
        firewall = "macos"  # the system asks to allow incoming connections
    elif not ufw_active:
        firewall = "open"  # no firewall in the way
    elif marker.exists():
        firewall = "configured"
    else:
        firewall = "needs-setup"
    from .capture import missing_dependencies

    lan = lan_addresses()
    linux = sys.platform.startswith("linux")
    capture_missing = missing_dependencies() if linux else []
    report = {
        "version": __version__,
        "python": sys.version.split()[0],
        "os": "windows" if IS_WINDOWS else "macos" if IS_MAC else "linux",
        "poppler": bool(shutil.which("pdftoppm") and shutil.which("pdfinfo")),
        "uxplay": bool(shutil.which("uxplay")),
        # Linux captures through the portal; elsewhere the share window does it in the browser.
        "capture": not capture_missing,
        "capture_mode": "native" if linux and not capture_missing else "browser",
        "avahi": bool(active("avahi-daemon")) if linux else True,
        "firewall": firewall,
        "lan": lan[0] if lan else "",
    }
    if args.json:
        print(json.dumps(report))
        return
    ok = lambda b: "ok" if b else "missing"
    setup = r"scripts\setup.ps1" if IS_WINDOWS else "scripts/setup"
    print(f"inkcast {report['version']} (python {report['python']}, {report['os']})")
    print(f"  PDF support (poppler)   {ok(report['poppler'])}")
    if report["capture_mode"] == "native":
        print("  Window annotation       ok (system window picker)")
    else:
        print("  Window annotation       via Chrome/Edge (click Window in the Inkcast window)")
    if linux:
        print(f"  AirPlay (uxplay)        {ok(report['uxplay'])}")
        print(f"  Discovery (avahi)       {ok(report['avahi'])}")
    elif IS_MAC:
        print("  AirPlay                 built into macOS (System Settings > AirPlay Receiver)")
    else:
        print("  AirPlay                 not supported on Windows")
    notes = {"open": "no firewall in the way", "configured": "rule added by setup",
             "needs-setup": "ufw may block the iPad (run setup)",
             "windows": "allow Python on private networks when Windows asks, or run setup",
             "macos": "allow incoming connections for Python if macOS asks"}
    print(f"  Firewall                {notes.get(firewall, firewall)}")
    print(f"  Network address         {report['lan'] or 'none (connect to Wi-Fi or Ethernet)'}")
    if not report["poppler"] or firewall in ("needs-setup", "windows"):
        print(f"\nRun {setup} to fix what's missing (it asks before each step).")


def user_uxplayrc():
    """Options from the user's own UxPlay startup file, if any."""
    for f in (os.environ.get("UXPLAYRC"), "~/.uxplayrc", "~/.config/uxplayrc"):
        if f and os.path.isfile(os.path.expanduser(f)):
            try:
                text = Path(os.path.expanduser(f)).read_text(errors="replace")
            except OSError:
                return []
            return [ln for ln in text.splitlines()
                    if ln.strip() and not ln.lstrip().startswith("#")
                    and ln.split()[0] not in ("pin", "pw", "reg", "rc")]
    return []


def pin_arg(value):
    if not re.fullmatch(r"\d{4}", value):
        raise argparse.ArgumentTypeError("PIN must be 4 digits")
    return value


def name_arg(value):
    value = value.strip()
    if not re.fullmatch(r"[\w .'-]{1,40}", value):
        raise argparse.ArgumentTypeError("use 1-40 letters, digits, spaces, . ' or -")
    return value


def main(argv=None):
    p = argparse.ArgumentParser(prog="inkcast", description="Apple Pencil whiteboard and iPad mirroring for your desktop.")
    p.add_argument("--version", action="version", version=f"inkcast {__version__}")
    sub = p.add_subparsers(dest="cmd")

    def common(sp):
        sp.add_argument("--port", type=port_arg, default=default_port(), help="HTTP port (default 8770 or $INKCAST_PORT)")
        sp.add_argument("--host", type=host_arg, help="LAN address to advertise to the iPad (auto-detected)")

    for name, fn, help_ in (
        ("board", cmd_board, "start if needed and open the viewer window (default)"),
        ("start", cmd_start, "start the whiteboard server in the background"),
        ("open", cmd_open, "open the viewer window (starts the server if needed)"),
        ("url", cmd_url, "print the iPad link and QR code"),
        ("stop", cmd_stop, "stop the background server"),
    ):
        sp = sub.add_parser(name, help=help_)
        common(sp)
        sp.set_defaults(fn=fn)

    sp = sub.add_parser("annotate", help="show a desktop window on the iPad to draw on (opens the window picker)")
    common(sp)
    sp.add_argument("--stop", action="store_true", help="stop the live window (its last frame stays)")
    sp.set_defaults(fn=cmd_annotate)

    sp = sub.add_parser("status", help="show whether the server is running")
    common(sp)
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(fn=cmd_status)

    sp = sub.add_parser("doctor", help="check dependencies, firewall and network")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(fn=cmd_doctor)

    sp = sub.add_parser("serve", help="run the whiteboard server in the foreground")
    common(sp)
    sp.add_argument("--bind", default="0.0.0.0", help="address to listen on (default 0.0.0.0; 127.0.0.1 = this computer only)")
    sp.add_argument("--quiet", action="store_true")
    sp.set_defaults(fn=cmd_serve)

    sp = sub.add_parser("mirror", help="AirPlay screen mirroring receiver (UxPlay)")
    sp.add_argument("--name", type=name_arg, default=os.environ.get("INKCAST_AIRPLAY_NAME", "Omarchy"))
    sp.add_argument("--pin", type=pin_arg, help="4-digit PIN (default: random)")
    sp.add_argument("--no-pin", action="store_true", help="let anyone on the network mirror without a PIN")
    sp.add_argument("--fullscreen", action="store_true")
    sp.add_argument("--no-audio", action="store_true", help="don't play the iPad's audio on this computer")
    sp.add_argument("uxplay_args", nargs=argparse.REMAINDER, help="extra arguments passed to uxplay")
    sp.set_defaults(fn=cmd_mirror)

    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0].startswith("--") and argv[0] not in ("--version", "--help"):
        argv = ["board", *argv]
    args = p.parse_args(argv)
    try:
        args.fn(args)
    except urllib.error.URLError as e:
        sys.exit(f"inkcast: could not reach the server: {e.reason}")
