#!/usr/bin/env python3
"""Inkcast test suite: security, protocol and desktop annotation.

Runs a real server on a free port with throwaway data directories; touches
nothing in your home folder. Needs poppler (pdfinfo/pdftoppm). The desktop
annotation tests use GStreamer's test pattern instead of a real window.

    python3 -B tests/run.py
"""

import base64
import json
import os
import shutil
import socket
import stat
import struct
import sys
import tempfile
import threading
import time

sys.dont_write_bytecode = True
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp(prefix="inkcast-test-")
os.environ["XDG_DATA_HOME"] = os.path.join(TMP, "data")
os.environ["XDG_RUNTIME_DIR"] = os.path.join(TMP, "run")
os.makedirs(os.environ["XDG_RUNTIME_DIR"], mode=0o700)
sys.path.insert(0, ROOT)

results = []


def check(name, cond, detail=""):
    results.append(bool(cond))
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail and not cond else ""))


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def tiny_pdf(pages=2):
    objs = ["<</Type/Catalog/Pages 2 0 R>>",
            f"<</Type/Pages/Kids[{' '.join(f'{3 + i} 0 R' for i in range(pages))}]/Count {pages}>>"]
    objs += ["<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]>>"] * pages
    out, offsets = "%PDF-1.4\n", []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{o}\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n" + "".join(f"{o:010d} 00000 n \n" for o in offsets)
    out += f"trailer\n<</Size {len(objs) + 1}/Root 1 0 R>>\nstartxref\n{xref}\n%%EOF\n"
    return out.encode()


# --------------------------------------------------------------------------- fake window source

try:
    import gi
    gi.require_version("Gst", "1.0")
    from gi.repository import Gst
    Gst.init(None)
    HAVE_GST = True
except (ImportError, ValueError):
    HAVE_GST = False


def jpeg_frames(w, h, n):
    p = Gst.parse_launch(f"videotestsrc num-buffers={n} pattern=smpte ! video/x-raw,width={w},height={h} "
                         "! jpegenc ! appsink name=s sync=false")
    sink = p.get_by_name("s")
    p.set_state(Gst.State.PLAYING)
    out = []
    while (smp := sink.emit("pull-sample")) is not None:
        b = smp.get_buffer()
        ok, m = b.map(Gst.MapFlags.READ)
        out.append(bytes(m.data))
        b.unmap(m)
    p.set_state(Gst.State.NULL)
    return out


class FakeCapture:
    """Stands in for WindowCapture: 'picks' a 640x400 window, then resizes it to 800x400."""

    def __init__(self, on_start, on_frame, on_resize, on_end, **_):
        self.cb = (on_start, on_frame, on_resize, on_end)
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self.thread.start()

    def stop(self):
        self.stopped.set()

    def _run(self):
        on_start, on_frame, on_resize, on_end = self.cb
        time.sleep(0.3)
        on_start(640, 400)
        for f in jpeg_frames(640, 400, 10):
            on_frame(f)
            time.sleep(0.05)
        on_resize(800, 400)
        for f in jpeg_frames(800, 400, 5):
            on_frame(f)
            time.sleep(0.05)
        self.stopped.wait(5)
        on_end("stopped")


import inkcast.capture as capture  # noqa: E402

capture.WindowCapture = FakeCapture
capture.missing_dependencies = lambda: [] if HAVE_GST else ["gstreamer"]

from inkcast.server import Hub, InkcastServer, write_private  # noqa: E402

PORT = free_port()
srv = InkcastServer("127.0.0.1", PORT)
threading.Thread(target=srv.serve_forever, daemon=True).start()
KEY = srv.token
DATA = os.path.join(os.environ["XDG_DATA_HOME"], "inkcast")
RUN = os.path.join(os.environ["XDG_RUNTIME_DIR"], "inkcast")

# --------------------------------------------------------------------------- clients


def http(method, path, headers=None, body=None, host=None):
    s = socket.create_connection(("127.0.0.1", PORT), timeout=10)
    h = {"Host": host or f"127.0.0.1:{PORT}", "Connection": "close", **(headers or {})}
    if body is not None:
        h["Content-Length"] = str(len(body))
    req = f"{method} {path} HTTP/1.1\r\n" + "".join(f"{k}: {v}\r\n" for k, v in h.items()) + "\r\n"
    s.sendall(req.encode() + (body or b""))
    data = b""
    while True:
        try:
            c = s.recv(65536)
        except socket.timeout:
            break
        if not c:
            break
        data += c
    s.close()
    head, _, rest = data.partition(b"\r\n\r\n")
    lines = head.decode(errors="replace").split("\r\n")
    code = int(lines[0].split()[1]) if lines and len(lines[0].split()) > 1 else 0
    hdrs = {ln.split(":", 1)[0].lower(): ln.split(":", 1)[1].strip() for ln in lines[1:] if ":" in ln}
    return code, hdrs, rest


def ws(path, origin=None, host=None):
    s = socket.create_connection(("127.0.0.1", PORT), timeout=5)
    k = base64.b64encode(os.urandom(16)).decode()
    extra = f"Origin: {origin}\r\n" if origin else ""
    s.sendall(f"GET {path} HTTP/1.1\r\nHost: {host or f'127.0.0.1:{PORT}'}\r\nUpgrade: websocket\r\n"
              f"Connection: Upgrade\r\nSec-WebSocket-Key: {k}\r\nSec-WebSocket-Version: 13\r\n{extra}\r\n".encode())
    buf = b""
    while b"\r\n\r\n" not in buf:
        c = s.recv(1)
        if not c:
            break
        buf += c
    return s, int(buf.split()[1]) if buf else 0


def send(s, payload, masked=True, binary=False):
    p = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    n, m = len(p), os.urandom(4)
    bit = 0x80 if masked else 0
    op = 0x82 if binary else 0x81
    if n < 126:
        h = bytes([op, bit | n])
    elif n < 65536:
        h = bytes([op, bit | 126]) + struct.pack("!H", n)
    else:
        h = bytes([op, bit | 127]) + struct.pack("!Q", n)
    if masked and n:
        p = (int.from_bytes(p, "big") ^ int.from_bytes((m * (n // 4 + 1))[:n], "big")).to_bytes(n, "big")
    s.sendall(h + (m if masked else b"") + p)


def rall(s, n):
    b = b""
    while len(b) < n:
        c = s.recv(n - len(b))
        if not c:
            raise ConnectionError
        b += c
    return b


def recv_any(s, timeout=3):
    """('text', obj) | ('binary', bytes) | ('closed', None)."""
    s.settimeout(timeout)
    while True:
        b1, b2 = rall(s, 2)
        n = b2 & 127
        if n == 126:
            n = struct.unpack("!H", rall(s, 2))[0]
        elif n == 127:
            n = struct.unpack("!Q", rall(s, 8))[0]
        d = rall(s, n)
        op = b1 & 15
        if op == 1:
            return "text", json.loads(d)
        if op == 2:
            return "binary", d
        if op == 8:
            return "closed", None


def recv_until(s, t, timeout=3):
    while True:
        kind, m = recv_any(s, timeout)
        if kind == "closed" or (kind == "text" and m.get("t") == t):
            return m or {}


def closed(s, timeout=3):
    s.settimeout(timeout)
    try:
        while True:
            d = s.recv(4096)
            if not d or d[:1] == b"\x88":
                return True
    except (socket.timeout, ConnectionError, OSError):
        return False


def mode(p):
    return stat.S_IMODE(os.stat(p).st_mode)


def wait_live_state(timeout=8):
    deadline = time.time() + timeout
    while time.time() < deadline and srv.hub.capture is not None:
        time.sleep(0.1)


# --------------------------------------------------------------------------- security

print("== access control")
code, _, body = http("GET", "/api/ping")
check("ping works without key and leaks nothing", code == 200 and b"inkcast" in body and b"pid" not in body)
check("data needs the key", http("GET", "/api/info")[0] == 403)
check("wrong key rejected", http("GET", "/api/info?k=AAAAAAAAAAAAAAAAAAAAAA")[0] == 403)
check("key in query works", http("GET", f"/api/info?k={KEY}")[0] == 200)
check("key in header works", http("GET", "/api/info", {"X-Inkcast-Key": KEY})[0] == 200)
check("theme.css needs key", http("GET", "/theme.css")[0] == 403)
check("qr needs key", http("GET", "/api/qr.svg")[0] == 403)
check("key is 128-bit", len(KEY) >= 22)

print("== DNS rebinding / cross-site")
check("foreign Host rejected", http("GET", f"/api/info?k={KEY}", host=f"evil.example:{PORT}")[0] == 421)
check("foreign Host rejected on pages", http("GET", "/view", host=f"attacker.test:{PORT}")[0] == 421)
check("localhost Host accepted", http("GET", "/view", host=f"localhost:{PORT}")[0] == 200)
check("ws without key rejected", ws("/ws?role=view")[1] == 403)
check("ws from foreign Origin rejected", ws(f"/ws?role=view&k={KEY}", origin="https://evil.example")[1] == 403)
check("ws from null Origin rejected", ws(f"/ws?role=view&k={KEY}", origin="null")[1] == 403)
check("upload from foreign Origin rejected",
      http("POST", f"/api/pdf?k={KEY}", {"Origin": "https://evil.example"}, b"%PDF-1.4")[0] == 403)
check("live start from foreign Origin rejected",
      http("POST", f"/api/live?k={KEY}", {"Origin": "https://evil.example"}, b'{"action":"start"}')[0] == 403)

print("== pages and files")
code, h, page = http("GET", "/view")
csp = h.get("content-security-policy", "")
check("strict CSP", "script-src 'self'" in csp and "style-src 'self'" in csp and "frame-ancestors 'none'" in csp)
check("nosniff, no-referrer, no framing",
      h.get("x-content-type-options") == "nosniff" and h.get("referrer-policy") == "no-referrer"
      and h.get("x-frame-options") == "DENY")
check("no inline styles (CSP-clean)", b"style=" not in page and b"style=" not in http("GET", "/pad")[2])
for p in ("/../inkcast/server.py", "/%2e%2e/inkcast/server.py", "/web/../inkcast/server.py", "//etc/passwd",
          "/pad.html", "/server.py"):
    check(f"no file outside the allowlist: {p}", http("GET", p)[0] in (403, 404))
check("server header doesn't reveal Python", "python" not in http("GET", "/api/ping")[1].get("server", "").lower())

print("== input validation")
view, code = ws(f"/ws?role=view&k={KEY}", origin=f"http://127.0.0.1:{PORT}")
check("ws with own Origin accepted", code == 101)
pid = recv_until(view, "state")["doc"]["pages"][0]["id"]
pad, _ = ws(f"/ws?role=pad&k={KEY}")
recv_until(pad, "state")
sid = "e" + os.urandom(4).hex()
send(pad, {"t": "begin", "page": pid, "s": {"id": sid, "color": "red;background:url(x)", "width": 1e9,
           "tool": "<script>", "z": "x", "pts": [[1, 2, 0.5], ["a", 2], [1e300, 5, 9], None, "x"], "extra": 1}})
s0 = recv_until(view, "begin").get("s", {})
check("color sanitized", s0.get("color") == "#111111", s0)
check("tool sanitized", s0.get("tool") == "pen")
check("width clamped", s0.get("width") == 80)
check("unknown keys dropped", "extra" not in s0)
check("points validated and clamped", s0.get("pts") == [[1, 2, 0.5], [100000.0, 5, 1]], s0.get("pts"))
for junk in ({"t": "pts", "id": sid, "pts": {"no": 1}}, {"t": "begin", "page": pid, "s": {"id": "../../etc"}},
             {"t": "paper", "page": pid, "paper": "<img src=x>"}, {"t": "goto", "index": True},
             {"t": "goto", "index": 10 ** 9}, {"t": "remove", "page": pid, "ids": "x"},
             {"t": "live", "action": "rm -rf"}):
    send(pad, junk)
send(pad, b"not json")
send(pad, b"[" * 100000 + b"]" * 100000)
send(pad, {"t": "cursor", "page": pid, "x": 1, "y": 2})
m = recv_until(view, "cursor")
check("junk ignored, connection alive", m.get("x") == 1 and m.get("y") == 2, m)
big, _ = ws(f"/ws?role=pad&k={KEY}")
recv_until(big, "state")
try:
    send(big, b"x" * (3 * 1024 * 1024))
    was_closed = closed(big)
except (BrokenPipeError, ConnectionResetError):
    was_closed = True
check("oversized message closes the connection", was_closed)
um, _ = ws(f"/ws?role=pad&k={KEY}")
recv_until(um, "state")
send(um, {"t": "cursor"}, masked=False)
check("unmasked client frame closes the connection", closed(um))

print("== control endpoints")
check("launch code needs key header", http("POST", "/api/launch")[0] == 403)
check("launch code ignores ?k=", http("POST", f"/api/launch?k={KEY}")[0] == 403)
check("shutdown needs key header", http("POST", "/api/shutdown")[0] == 403)
check("shutdown ignores ?k=", http("POST", f"/api/shutdown?k={KEY}")[0] == 403)
code, _, body = http("POST", "/api/launch", {"X-Inkcast-Key": KEY})
lc = json.loads(body)["c"]
code, _, body = http("POST", "/api/redeem", {"Content-Type": "application/json"}, json.dumps({"c": lc}).encode())
check("launch code redeems to the key", code == 200 and json.loads(body)["k"] == KEY)
check("launch code is single-use",
      http("POST", "/api/redeem", {"Content-Type": "application/json"}, json.dumps({"c": lc}).encode())[0] == 403)
check("redeem from foreign Origin rejected",
      http("POST", "/api/redeem", {"Origin": "https://evil.example"}, b'{"c":"x"}')[0] == 403)
check("live control needs key", http("POST", "/api/live", body=b'{"action":"start"}')[0] == 403)

print("== PDFs")
check("upload needs key", http("POST", "/api/pdf", body=b"%PDF-1.4")[0] == 403)
check("non-PDF rejected", http("POST", f"/api/pdf?k={KEY}", body=b"#!/bin/sh\nrm -rf ~\n")[0] == 400)
code, _, body = http("POST", f"/api/pdf?k={KEY}&name=../../x%0a.pdf", body=tiny_pdf(2))
check("real PDF accepted", code == 200, body)
st = recv_until(view, "state")
name = st["doc"]["pdf"]["name"]
check("PDF name sanitized", "/" not in name and "\n" not in name, name)
rev = st["doc"]["pdf"]["rev"]
check("page render needs key", http("GET", f"/api/pdf/{rev}/1.png")[0] == 403)
check("page renders", http("GET", f"/api/pdf/{rev}/1.png?k={KEY}")[0] == 200)
check("page out of range 404", http("GET", f"/api/pdf/{rev}/3.png?k={KEY}")[0] == 404)
check("other PDF 404", http("GET", f"/api/pdf/aaaaaaaaaaaa/1.png?k={KEY}")[0] == 404)

# --------------------------------------------------------------------------- desktop annotation

print("== desktop annotation")
if not HAVE_GST:
    print("SKIP GStreamer not available")
else:
    send(pad, {"t": "live", "action": "start"})
    send(pad, {"t": "live", "action": "start"})  # a second request while picking is ignored
    seen = {"picking": False, "live": False, "frames": 0, "jpeg": False, "page": None, "current": False}
    deadline = time.time() + 8
    while time.time() < deadline and seen["frames"] < 12:
        try:
            kind, m = recv_any(pad, 2)
        except (OSError, ConnectionError):
            break
        if kind == "binary":
            seen["frames"] += 1
            seen["jpeg"] = m[:2] == b"\xff\xd8"
        elif m.get("t") == "live":
            seen["picking"] |= m.get("state") == "picking"
            seen["live"] |= m.get("state") == "live"
        elif m.get("t") == "state":
            lp = [p for p in m["doc"]["pages"] if p["bg"] and p["bg"].get("live")]
            if lp:
                seen["page"] = lp[0]["id"]
                seen["current"] = m["doc"]["pages"][m["doc"]["page"]]["id"] == lp[0]["id"]
    check("picker requested", seen["picking"])
    check("window goes live", seen["live"])
    check("live window becomes the current page", seen["page"] and seen["current"])
    check("JPEG frames streamed to the iPad", seen["frames"] >= 10 and seen["jpeg"], seen["frames"])
    time.sleep(0.5)
    status = json.load(open(os.path.join(RUN, "status.json")))
    check("bar status reports live", status.get("live") == "live", status.get("live"))
    code, _, body = http("GET", f"/api/live/frame.jpg?k={KEY}")
    check("current frame available for PDF export", code == 200 and body[:2] == b"\xff\xd8")
    check("current frame needs key", http("GET", "/api/live/frame.jpg")[0] == 403)
    time.sleep(0.8)
    late, _ = ws(f"/ws?role=view&k={KEY}")
    kind, m = recv_any(late)
    lp = [p for p in m["doc"]["pages"] if p["bg"] and p["bg"].get("live")][0]
    check("window resize updates the page shape", lp["h"] == 500.0, lp["h"])
    kind, _ = recv_any(late)
    while kind == "text":
        kind, _ = recv_any(late)
    check("a late joiner gets the current frame immediately", kind == "binary")
    live_id = lp["id"]
    send(pad, {"t": "live", "action": "stop"})
    end = None
    for _ in range(60):
        kind, m = recv_any(late, 6)
        if kind == "text" and m.get("t") == "live" and m.get("state") == "idle":
            end = m
        if kind == "text" and m.get("t") == "state" and end:
            break
    check("stop reports why", end and end.get("reason") == "stopped", end)
    pg = [p for p in m["doc"]["pages"] if p["id"] == live_id][0]
    check("stopped window keeps its last frame", pg["bg"] == {"img": live_id}, pg["bg"])
    code, _, body = http("GET", f"/api/img/{live_id}.jpg?k={KEY}")
    check("frozen frame served", code == 200 and body[:2] == b"\xff\xd8")
    check("frozen frame needs key", http("GET", f"/api/img/{live_id}.jpg")[0] == 403)
    check("unknown frame id 404", http("GET", f"/api/img/nope.jpg?k={KEY}")[0] == 404)
    check("frozen frame stored owner-only", mode(os.path.join(DATA, f"img-{live_id}.jpg")) == 0o600)

    send(pad, {"t": "live", "action": "start"})
    time.sleep(0.8)
    send(pad, {"t": "delpage", "page": srv.hub.live["page"]})
    wait_live_state()
    check("deleting the live page stops capture", srv.hub.capture is None and srv.hub.live["state"] == "idle",
          srv.hub.live)
    send(pad, {"t": "newdoc"})
    time.sleep(0.5)
    check("a new notebook deletes old frames", not any(f.startswith("img-") for f in os.listdir(DATA)))

    doc = {"id": "d1", "page": 0, "pdf": None, "pages": [{"id": "pg1", "w": 1000, "h": 500, "paper": "blank",
           "bg": {"live": True}, "pt": [600, 300], "strokes": []}]}
    write_private(srv.store / "img-pg1.jpg", b"\xff\xd8\xff\xd9")
    write_private(srv.store / "session.json", json.dumps(doc))
    check("after a restart a live page shows its last frame",
          Hub(srv.store).doc["pages"][0]["bg"] == {"img": "pg1"})

# --------------------------------------------------------------------------- browser capture

print("== desktop annotation through the browser (Windows/macOS)")
srv.hub._native = False  # as on Windows/macOS: the share window captures


def drain(sock, want, timeout=4):
    """Collect messages until `want(kind, msg)` is true or time runs out."""
    deadline = time.time() + timeout
    got = []
    while time.time() < deadline:
        try:
            kind, m = recv_any(sock, max(0.1, deadline - time.time()))
        except (OSError, ConnectionError):
            break
        got.append((kind, m))
        if want(kind, m):
            break
    return got


bpad, _ = ws(f"/ws?role=pad&k={KEY}")
st = recv_until(bpad, "state")
check("clients are told capture happens in the browser", st.get("capture") == "browser", st.get("capture"))
share, _ = ws(f"/ws?role=view&k={KEY}")
recv_until(share, "state")
send(bpad, {"t": "live", "action": "start"})
m = recv_until(share, "live")
check("an iPad start asks the share window for a click", m.get("state") == "needs-desktop", m)
send(bpad, {"t": "live", "action": "source", "w": 800, "h": 500})
time.sleep(0.4)
check("the iPad can't make itself the capture source", srv.hub.source is None)
send(share, {"t": "live", "action": "source", "w": 800, "h": 500})
got = drain(bpad, lambda k, m: k == "text" and m.get("t") == "live" and m.get("state") == "live")
check("a share window becomes the source and the page goes live",
      srv.hub.source is not None and any(k == "text" and m.get("state") == "live" for k, m in got))
jpeg = jpeg_frames(320, 200, 1)[0] if HAVE_GST else b"\xff\xd8\xff\xe0" + b"\0" * 64 + b"\xff\xd9"
send(share, jpeg, binary=True)
got = drain(bpad, lambda k, m: k == "binary")
check("its JPEG frames reach the iPad", any(k == "binary" and m == jpeg for k, m in got))
send(share, b"<script>alert(1)</script>", binary=True)
got = drain(bpad, lambda k, m: k == "binary", timeout=1)
check("non-JPEG data is not relayed", not any(k == "binary" for k, m in got))
other, _ = ws(f"/ws?role=view&k={KEY}")
recv_until(other, "state")
send(other, {"t": "live", "action": "source", "w": 640, "h": 480})
m = recv_until(other, "live")
check("a second source is refused", m.get("state") == "error", m)
send(share, {"t": "live", "action": "resize", "w": 1000, "h": 500})
time.sleep(0.4)
live_page = srv.hub.pages.get(srv.hub.live["page"])
check("the source can report a new window size", live_page and live_page["h"] == 500.0, live_page and live_page["h"])
pid_live = srv.hub.live["page"]
share.close()
got = drain(bpad, lambda k, m: k == "text" and m.get("t") == "live" and m.get("state") == "idle")
end = [m for k, m in got if k == "text" and m.get("t") == "live" and m.get("state") == "idle"]
check("closing the share window ends capture", end and end[0].get("reason") == "closed", end)
time.sleep(0.3)
check("and the page keeps the last frame", srv.hub.pages[pid_live]["bg"] == {"img": pid_live})
srv.hub._native = None

# --------------------------------------------------------------------------- impostor servers

print("== the CLI only gives the key to the real server")
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer  # noqa: E402

import inkcast.cli as cli  # noqa: E402
from inkcast.server import ping_proof  # noqa: E402

nonce = "ab" * 16
code, _, body = http("GET", f"/api/ping?n={nonce}")
check("server proves it holds the key", json.loads(body).get("proof") == ping_proof(KEY, nonce))
check("no proof without a valid challenge", "proof" not in json.loads(http("GET", "/api/ping?n=zz")[2]))
check("CLI recognizes the real server", cli.probe(PORT) == "ours")

seen_headers = []


def squatter(proof):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            seen_headers.append(dict(self.headers))
            out = {"app": "inkcast", "version": "9"}
            if proof:
                out["proof"] = proof
            body = json.dumps(out).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_POST = do_GET

    s = ThreadingHTTPServer(("127.0.0.1", free_port()), H)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    return s


for label, proof in (("no proof", None), ("a forged proof", "0" * 64)):
    fake = squatter(proof)
    fport = fake.server_address[1]
    check(f"impostor with {label} is detected", cli.probe(fport) == "other")
    try:
        cli.request(fport, "/api/launch", "POST")
        refused = False
    except SystemExit:
        refused = True
    check(f"CLI refuses to send the key to an impostor with {label}", refused)
    check(f"start doesn't treat an impostor with {label} as running",
          cli.start_background(type("A", (), {"port": fport, "host": None})()) is False)
    fake.shutdown()
check("no key was ever sent to the impostors",
      seen_headers and not any(KEY in json.dumps(h) for h in seen_headers), len(seen_headers))
check("nothing listening means not running", cli.probe(free_port()) is None)

# --------------------------------------------------------------------------- command lines

print("== secrets stay off command lines")
# Other users can read every process's arguments (/proc/<pid>/cmdline), so the
# key, launch codes and the AirPlay PIN must never be passed as arguments.
import subprocess  # noqa: E402

import inkcast.cli as cli  # noqa: E402

argvs = []
real_run, real_popen = subprocess.run, subprocess.Popen


def spy_run(cmd, *a, **kw):
    argvs.append(list(cmd))
    return real_run(cmd, *a, **kw)


class SpyPopen(real_popen):
    def __init__(self, cmd, *a, **kw):
        argvs.append(list(cmd))
        super().__init__(cmd, *a, **kw)


subprocess.run, subprocess.Popen = spy_run, SpyPopen
try:
    http("GET", f"/api/qr.svg?k={KEY}")
    srv.write_status()
    import io
    real_stdout = cli.sys.stdout
    cli.sys.stdout = type("Quiet", (), {"buffer": io.BytesIO(), "write": lambda *a: None, "flush": lambda *a: None})()
    cli.print_qr(srv.pad_urls()[0] if srv.pad_urls() else f"http://127.0.0.1:{PORT}/?k={KEY}")
    opened = []
    cli.open_window = lambda url: opened.append(url)
    real_request = cli.request
    cli.request = lambda port, path, method="GET", timeout=3: {"c": srv.new_launch_code()}
    cli.cmd_open(type("A", (), {"port": PORT, "host": None})())
    cli.request = real_request
    mirror_cmd = []
    if shutil.which("uxplay") and sys.platform != "darwin":
        real_exec = os.execvp
        os.execvp = lambda f, c: mirror_cmd.extend(c)
        cli.cmd_mirror(type("A", (), {"name": "Test", "pin": "4826", "no_pin": False, "fullscreen": False,
                                      "no_audio": False, "uxplay_args": []})())
        os.execvp = real_exec
finally:
    subprocess.run, subprocess.Popen = real_run, real_popen
    cli.sys.stdout = real_stdout
check("QR codes are made in-process (no helper program sees the link)",
      not any(a and "qrencode" in a[0] for a in argvs))
check("the key never appears in a process's arguments", not any(KEY in " ".join(a) for a in argvs))
check("the browser gets a file path, not a URL with a code",
      len(opened) == 1 and opened[0].startswith("file://") and "c=" not in opened[0] and KEY not in opened[0], opened)
launcher = opened[0][len("file://"):] if opened else ""
check("launcher page is owner-only", launcher and mode(launcher) == 0o600)
if mirror_cmd:
    check("AirPlay PIN is not in UxPlay's arguments", "4826" not in " ".join(mirror_cmd), mirror_cmd)
    rc = mirror_cmd[mirror_cmd.index("-rc") + 1] if "-rc" in mirror_cmd else ""
    check("PIN goes through an owner-only UxPlay options file",
          rc and mode(rc) == 0o600 and "pin 4826" in open(rc).read())
else:
    print("SKIP uxplay not installed")

# --------------------------------------------------------------------------- files

print("== file permissions")
time.sleep(2.5)
check("data dir 0700", mode(DATA) == 0o700, oct(mode(DATA)))
check("token 0600", mode(os.path.join(DATA, "token")) == 0o600)
check("session 0600", mode(os.path.join(DATA, "session.json")) == 0o600)
check("runtime dir 0700", mode(RUN) == 0o700)
check("status 0600", mode(os.path.join(RUN, "status.json")) == 0o600)

srv.shutdown()
srv.server_close()
print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
