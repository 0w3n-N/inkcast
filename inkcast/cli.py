"""inkcast command line: board, serve, stop, url, mirror."""

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from . import __version__

DEFAULT_PORT = int(os.environ.get("INKCAST_PORT", "8770"))
IS_MAC = sys.platform == "darwin"


def state_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    if IS_MAC:
        base = os.path.expanduser("~/Library/Logs")
    d = Path(base) / "inkcast"
    d.mkdir(parents=True, exist_ok=True)
    return d


def local(port, path):
    return f"http://127.0.0.1:{port}{path}"


def ping(port):
    try:
        with urllib.request.urlopen(local(port, "/api/ping"), timeout=0.5) as r:
            return json.load(r).get("app") == "inkcast"
    except (OSError, ValueError):
        return False


def info(port):
    with urllib.request.urlopen(local(port, "/api/info"), timeout=2) as r:
        return json.load(r)


def print_pad_urls(port):
    urls = info(port)["urls"]
    if not urls:
        print("No LAN address found. Is Wi-Fi connected? You can pass --host <ip>.")
        return
    print("On your iPad, open (same Wi-Fi):\n")
    for u in urls:
        print(f"  {u}")
    print(flush=True)
    if shutil.which("qrencode"):
        subprocess.run(["qrencode", "-t", "ANSIUTF8", "-m", "2", urls[0]])


def open_window(url):
    """Open the viewer as a chrome-less app window (screen-share friendly)."""
    if shutil.which("omarchy-launch-webapp"):
        subprocess.Popen(["omarchy-launch-webapp", url], start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return
    for b in ("chromium", "google-chrome-stable", "google-chrome", "brave", "microsoft-edge-stable"):
        if shutil.which(b):
            subprocess.Popen([b, f"--app={url}"], start_new_session=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
    if IS_MAC:
        for app in ("Google Chrome", "Chromium", "Brave Browser", "Microsoft Edge"):
            if Path(f"/Applications/{app}.app").exists():
                subprocess.Popen(["open", "-na", app, "--args", f"--app={url}"])
                return
    import webbrowser
    webbrowser.open(url)


def start_background(args):
    log = state_dir() / "server.log"
    cmd = [sys.executable, "-m", "inkcast", "serve", "--port", str(args.port), "--quiet"]
    if args.host:
        cmd += ["--host", args.host]
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parent.parent)}
    with open(log, "ab") as f:
        subprocess.Popen(cmd, stdout=f, stderr=f, stdin=subprocess.DEVNULL,
                         start_new_session=True, env=env)
    for _ in range(50):
        if ping(args.port):
            return True
        time.sleep(0.1)
    print(f"inkcast: server did not start, see {log}", file=sys.stderr)
    return False


def cmd_serve(args):
    from .server import InkcastServer

    try:
        srv = InkcastServer(args.bind, args.port, args.host)
    except OSError as e:
        sys.exit(f"inkcast: cannot listen on port {args.port}: {e}")
    if not args.quiet:
        print(f"Inkcast {__version__} listening on port {args.port}")
        print(f"Viewer window: {local(args.port, '/view')}")
        for u in srv.pad_urls():
            print(f"iPad:          {u}")
        sys.stdout.flush()
        if shutil.which("qrencode") and srv.pad_urls():
            subprocess.run(["qrencode", "-t", "ANSIUTF8", "-m", "2", srv.pad_urls()[0]])
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


def cmd_board(args):
    if not ping(args.port) and not start_background(args):
        sys.exit(1)
    open_window(local(args.port, "/view"))
    if sys.stdout.isatty():
        print_pad_urls(args.port)


def cmd_stop(args):
    if not ping(args.port):
        print("inkcast is not running")
        return
    req = urllib.request.Request(local(args.port, "/api/shutdown"), method="POST")
    urllib.request.urlopen(req, timeout=2).read()
    print("inkcast stopped")


def cmd_url(args):
    if not ping(args.port):
        sys.exit("inkcast is not running (start it with: inkcast board)")
    print_pad_urls(args.port)


def cmd_mirror(args):
    """AirPlay screen mirroring via UxPlay."""
    if IS_MAC:
        sys.exit("macOS has a built-in AirPlay receiver: System Settings > General > AirDrop & Handoff > AirPlay Receiver.")
    if not shutil.which("uxplay"):
        sys.exit("UxPlay is not installed. Run: scripts/install.sh --mirror  (or: omarchy pkg aur add uxplay)")
    if subprocess.run(["systemctl", "is-active", "--quiet", "avahi-daemon"]).returncode != 0:
        print("warning: avahi-daemon is not running; the iPad will not discover this computer.\n"
              "         enable it with: sudo systemctl enable --now avahi-daemon", file=sys.stderr)
    # -p: fixed legacy ports (TCP 7000 7001 7100, UDP 6000 6001 7011) so the firewall rules match.
    # -nh: do not append "@hostname" to the AirPlay name.
    cmd = ["uxplay", "-n", args.name, "-nh", "-p"]
    if args.fullscreen:
        cmd.append("-fs")
    if args.no_audio:
        cmd += ["-as", "0"]
    cmd += args.uxplay_args
    print(f"AirPlay receiver \"{args.name}\" is running.")
    print("On the iPad: Control Center > Screen Mirroring > " + args.name + ".  Ctrl+C to stop.")
    os.execvp(cmd[0], cmd)


def main(argv=None):
    p = argparse.ArgumentParser(prog="inkcast", description="Apple Pencil whiteboard and iPad mirroring for your desktop.")
    p.add_argument("--version", action="version", version=f"inkcast {__version__}")
    sub = p.add_subparsers(dest="cmd")

    def common(sp):
        sp.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"HTTP port (default {DEFAULT_PORT})")
        sp.add_argument("--host", help="LAN address to advertise to the iPad (auto-detected)")

    sp = sub.add_parser("board", help="start the whiteboard and open the viewer window (default)")
    common(sp)
    sp.set_defaults(fn=cmd_board)

    sp = sub.add_parser("serve", help="run the whiteboard server in the foreground")
    common(sp)
    sp.add_argument("--bind", default="0.0.0.0", help="address to listen on (default 0.0.0.0)")
    sp.add_argument("--quiet", action="store_true")
    sp.set_defaults(fn=cmd_serve)

    sp = sub.add_parser("url", help="print the iPad URL and QR code")
    common(sp)
    sp.set_defaults(fn=cmd_url)

    sp = sub.add_parser("stop", help="stop the background server")
    common(sp)
    sp.set_defaults(fn=cmd_stop)

    sp = sub.add_parser("mirror", help="AirPlay screen mirroring receiver (UxPlay)")
    sp.add_argument("--name", default=os.environ.get("INKCAST_AIRPLAY_NAME", "Omarchy"))
    sp.add_argument("--fullscreen", action="store_true")
    sp.add_argument("--no-audio", action="store_true", help="do not play the iPad's audio on this computer")
    sp.add_argument("uxplay_args", nargs=argparse.REMAINDER, help="extra arguments passed to uxplay")
    sp.set_defaults(fn=cmd_mirror)

    args = p.parse_args(argv)
    if not args.cmd:
        args = p.parse_args(["board", *(argv or sys.argv[1:])])
    args.fn(args)
