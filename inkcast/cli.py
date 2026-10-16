"""inkcast command line."""

import argparse
import ipaddress
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import __version__
from .server import data_dir, private_dir, runtime_dir

IS_MAC = sys.platform == "darwin"


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


def request(port, path, method="GET", timeout=3):
    req = urllib.request.Request(local(port, path), method=method,
                                 headers={"X-Inkcast-Key": token()})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def ping(port):
    try:
        with urllib.request.urlopen(local(port, "/api/ping"), timeout=0.5) as r:
            return json.load(r).get("app") == "inkcast"
    except (OSError, ValueError):
        return False


def print_pad_urls(port):
    urls = request(port, "/api/info")["urls"]
    if not urls:
        print("No Wi-Fi/Ethernet address found. Connect to a network, or pass --host <ip>.")
        return
    print("On your iPad, scan this or open the link in Safari (same Wi-Fi):\n")
    for u in urls:
        print(f"  {u}")
    print(flush=True)
    if shutil.which("qrencode"):
        subprocess.run(["qrencode", "-t", "ANSIUTF8", "-m", "2", urls[0]])


def open_window(url):
    """Open the viewer as a chrome-less app window (screen-share friendly)."""
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
    if ping(args.port):
        return True
    log = log_dir() / "server.log"
    cmd = [sys.executable, "-m", "inkcast", "serve", "--port", str(args.port), "--quiet"]
    if args.host:
        cmd += ["--host", args.host]
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parent.parent)}
    fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "ab") as f:
        subprocess.Popen(cmd, stdout=f, stderr=f, stdin=subprocess.DEVNULL,
                         start_new_session=True, env=env)
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
        if shutil.which("qrencode") and srv.pad_urls():
            subprocess.run(["qrencode", "-t", "ANSIUTF8", "-m", "2", srv.pad_urls()[0]])
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


def cmd_start(args):
    if not start_background(args):
        sys.exit(1)


def cmd_open(args):
    if not start_background(args):
        sys.exit(1)
    code = request(args.port, "/api/launch", "POST")["c"]
    open_window(local(args.port, f"/view?c={code}"))


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
        # -reg remembers devices that entered it, so they aren't asked again.
        pin = args.pin or f"{secrets.randbelow(10000):04d}"
        cmd += ["-pin", pin, "-reg", str(data_dir() / "airplay-devices")]
    # Let the bar plugin show the name and PIN (owner-only file).
    from .server import write_private
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
    if not ufw_active:
        firewall = "open"  # no firewall in the way
    elif marker.exists():
        firewall = "configured"
    else:
        firewall = "needs-setup"
    lan = lan_addresses()
    report = {
        "version": __version__,
        "python": sys.version.split()[0],
        "poppler": bool(shutil.which("pdftoppm") and shutil.which("pdfinfo")),
        "qrencode": bool(shutil.which("qrencode")),
        "uxplay": bool(shutil.which("uxplay")),
        "avahi": bool(active("avahi-daemon")) if not IS_MAC else True,
        "firewall": firewall,
        "lan": lan[0] if lan else "",
    }
    if args.json:
        print(json.dumps(report))
        return
    ok = lambda b: "ok" if b else "missing"
    print(f"inkcast {report['version']} (python {report['python']})")
    print(f"  PDF support (poppler)   {ok(report['poppler'])}")
    print(f"  QR codes (qrencode)     {ok(report['qrencode'])}")
    print(f"  AirPlay (uxplay)        {ok(report['uxplay'])}")
    print(f"  Discovery (avahi)       {ok(report['avahi'])}")
    print(f"  Firewall                {report['firewall']}")
    print(f"  Network address         {report['lan'] or 'none'}")
    if not all([report["poppler"], report["qrencode"]]) or firewall == "needs-setup":
        print("\nRun scripts/setup to fix what's missing (it asks before each step).")


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
