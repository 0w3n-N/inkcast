#!/usr/bin/env python3
"""Cross-platform smoke test (Linux, macOS, Windows): the real CLI starts the
background server, a client pairs and connects, then the CLI stops it.

    python3 -B tests/smoke.py
"""

import base64
import json
import os
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request

sys.dont_write_bytecode = True
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp(prefix="inkcast-smoke-")
os.environ["XDG_DATA_HOME"] = os.path.join(TMP, "data")
os.environ["XDG_STATE_HOME"] = os.path.join(TMP, "state")
os.environ["LOCALAPPDATA"] = os.path.join(TMP, "local")  # Windows data folder
os.environ.pop("XDG_RUNTIME_DIR", None)
sys.path.insert(0, ROOT)

import inkcast.cli as cli  # noqa: E402
from inkcast import qr  # noqa: E402
from inkcast.server import data_dir  # noqa: E402

results = []


def check(name, cond, detail=""):
    results.append(bool(cond))
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail and not cond else ""), flush=True)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


PORT = free_port()
INKCAST = [sys.executable, "-B", os.path.join(ROOT, "bin", "inkcast")]


HOST = []  # set below if this machine has no LAN address (some CI VMs)


def run(*args, port=True):
    extra = ["--port", str(PORT)] + HOST if port else []
    return subprocess.run(INKCAST + list(args) + extra, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=90, env=os.environ)


print(f"python {sys.version.split()[0]} on {sys.platform}; data in {data_dir()}")

# QR encoder works everywhere (no external program)
rows = qr.matrix("http://192.168.1.2:8770/?k=abcdefghijklmnopqrstuv")
check("QR encoder", len(rows) == len(rows[0]) and len(rows) >= 25)

# doctor
r = run("doctor", "--json", port=False)
doc = json.loads(r.stdout or "{}")
check("doctor runs", r.returncode == 0 and doc.get("version"), r.stderr[-300:])
check("doctor reports the OS", doc.get("os") in ("linux", "macos", "windows"), doc)
if not doc.get("lan"):
    print("no LAN address on this machine; advertising 127.0.0.1 with --host")
    HOST = ["--host", "127.0.0.1"]

# the CLI starts the background server (detached process, per-OS flags)
r = run("start")
check("inkcast start", r.returncode == 0, r.stderr[-500:])
if r.returncode != 0:
    log = cli.log_dir() / "server.log"
    print("server.log:\n" + (log.read_text(errors="replace")[-3000:] if log.exists() else "(missing)"))
check("the server proves it holds the key", cli.probe(PORT) == "ours")
key = (data_dir() / "token").read_text().strip()
check("key file exists", len(key) >= 22)

r = run("status", "--json")
check("inkcast status", r.returncode == 0 and json.loads(r.stdout).get("running") is True, r.stdout + r.stderr)

r = run("url")
check("inkcast url prints the link and a QR code", r.returncode == 0 and "/?k=" in r.stdout and "▀" in r.stdout,
      r.stdout[-300:] + r.stderr[-300:])

# HTTP + WebSocket like the iPad
with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/?k={key}", timeout=5) as resp:
    check("pad page served", resp.status == 200 and b"pad.js" in resp.read())
with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/qr.svg?k={key}", timeout=5) as resp:
    check("QR image served", resp.read().startswith(b"<svg"))

s = socket.create_connection(("127.0.0.1", PORT), timeout=5)
k = base64.b64encode(os.urandom(16)).decode()
s.sendall(f"GET /ws?role=pad&k={key} HTTP/1.1\r\nHost: 127.0.0.1:{PORT}\r\nUpgrade: websocket\r\n"
          f"Connection: Upgrade\r\nSec-WebSocket-Key: {k}\r\nSec-WebSocket-Version: 13\r\n\r\n".encode())
buf = b""
while b"\r\n\r\n" not in buf:
    buf += s.recv(1)
check("WebSocket upgrade", b" 101 " in buf.split(b"\r\n")[0])
b1, b2 = s.recv(2)
n = b2 & 127
if n == 126:
    n = struct.unpack("!H", s.recv(2))[0]
data = b""
while len(data) < n:
    data += s.recv(n - len(data))
state = json.loads(data)
check("document state received", state.get("t") == "state" and state["doc"]["pages"], state.get("t"))
check("capture mode reported", state.get("capture") in ("native", "browser"), state.get("capture"))
s.close()

# opening the share window: an owner-only launcher page, never a URL with a code
opened = []
cli.open_window = lambda url: opened.append(url)
cli.cmd_open(type("A", (), {"port": PORT, "host": None})())
check("share window gets a file:// launcher page", opened and opened[0].startswith("file:"), opened)
launcher = urllib.request.url2pathname(urllib.parse.urlparse(opened[0]).path) if opened else ""
check("launcher page is on disk until it's used", launcher and os.path.isfile(launcher), launcher)

r = run("stop")
check("inkcast stop", r.returncode == 0 and cli.probe(PORT) is None, r.stdout + r.stderr)

# the Windows scripts at least parse
if os.name == "nt":
    for script in ("setup.ps1", "uninstall.ps1"):
        path = os.path.join(ROOT, "scripts", script)
        ps = ("$errs = $null; [void][System.Management.Automation.Language.Parser]::ParseFile("
              f"'{path}', [ref]$null, [ref]$errs); if ($errs) {{ $errs | Out-String; exit 1 }}")
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True)
        check(f"{script} parses in Windows PowerShell", r.returncode == 0, r.stdout + r.stderr)
    r = subprocess.run([os.path.join(ROOT, "bin", "inkcast.cmd"), "--version"], capture_output=True, text=True)
    check("bin\\inkcast.cmd runs", r.returncode == 0 and "inkcast" in r.stdout, r.stdout + r.stderr)

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
