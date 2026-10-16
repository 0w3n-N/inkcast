"""Inkcast server: a tiny LAN whiteboard hub.

Standard library only. Serves the iPad pad UI and the desktop viewer, relays
pen strokes over WebSockets, keeps the document in memory (persisted to disk),
and rasterizes PDF pages with poppler's `pdftoppm`.
"""

import base64
import hashlib
import hmac
import json
import os
import queue
import re
import secrets
import shutil
import socket
import struct
import subprocess
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
PAGE_W = 1000  # page coordinates: width is always 1000 units
MAX_PDF_BYTES = 300 * 1024 * 1024
PDF_RENDER_PX = 2000

MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".json": "application/json",
}


def data_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    if os.uname().sysname == "Darwin" and "XDG_DATA_HOME" not in os.environ:
        base = os.path.expanduser("~/Library/Application Support")
    d = Path(base) / "inkcast"
    d.mkdir(parents=True, exist_ok=True)
    return d


def new_id() -> str:
    return secrets.token_hex(6)


def blank_page(aspect=9 / 16, paper="grid", pt=(960, 540)):
    return {"id": new_id(), "w": PAGE_W, "h": round(PAGE_W * aspect, 2), "paper": paper,
            "bg": None, "pt": list(pt), "strokes": []}


def blank_doc():
    return {"id": new_id(), "pages": [blank_page()], "page": 0, "pdf": None}


# --------------------------------------------------------------------------- WebSocket


class WSConn:
    def __init__(self, sock, rfile, role, addr):
        self.sock = sock
        self.rfile = rfile
        self.role = role
        self.addr = addr
        self.id = new_id()
        self.out = queue.Queue()
        self.alive = True
        threading.Thread(target=self._writer, daemon=True).start()

    def _writer(self):
        while self.alive:
            data = self.out.get()
            if data is None:
                break
            try:
                self.sock.sendall(data)
            except OSError:
                self.alive = False
                break

    @staticmethod
    def _frame(opcode, payload: bytes) -> bytes:
        n = len(payload)
        if n < 126:
            head = struct.pack("!BB", 0x80 | opcode, n)
        elif n < 65536:
            head = struct.pack("!BBH", 0x80 | opcode, 126, n)
        else:
            head = struct.pack("!BBQ", 0x80 | opcode, 127, n)
        return head + payload

    def send(self, obj):
        if not self.alive:
            return
        if self.out.qsize() > 20000:  # hopelessly behind; drop it
            self.close()
            return
        self.out.put(self._frame(0x1, json.dumps(obj, separators=(",", ":")).encode()))

    def close(self):
        if self.alive:
            self.alive = False
            try:
                self.sock.sendall(self._frame(0x8, b""))
            except OSError:
                pass
        self.out.put(None)

    def _read(self, n):
        buf = self.rfile.read(n)
        if len(buf) < n:
            raise ConnectionError("eof")
        return buf

    def recv(self):
        """Return the next text message, or None when the socket closes."""
        parts = []
        while True:
            b1, b2 = self._read(2)
            fin, opcode = b1 & 0x80, b1 & 0x0F
            n = b2 & 0x7F
            if n == 126:
                n = struct.unpack("!H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack("!Q", self._read(8))[0]
            if n > 64 * 1024 * 1024:
                raise ConnectionError("frame too large")
            mask = self._read(4) if b2 & 0x80 else None
            data = self._read(n) if n else b""
            if mask and n:
                key = (mask * (n // 4 + 1))[:n]
                data = (int.from_bytes(data, "big") ^ int.from_bytes(key, "big")).to_bytes(n, "big")
            if opcode == 0x8:
                return None
            if opcode == 0x9:
                self.out.put(self._frame(0xA, data))
                continue
            if opcode == 0xA:
                continue
            parts.append(data)
            if fin:
                return b"".join(parts).decode("utf-8", "replace")


# --------------------------------------------------------------------------- Document hub


class Hub:
    def __init__(self, store: Path):
        self.store = store
        self.lock = threading.RLock()
        self.conns: list[WSConn] = []
        self.doc = self._load()
        self._index()
        self._dirty = False
        threading.Thread(target=self._saver, daemon=True).start()

    # persistence
    def _load(self):
        f = self.store / "session.json"
        try:
            doc = json.loads(f.read_text())
            if doc.get("pages"):
                return doc
        except (OSError, ValueError):
            pass
        return blank_doc()

    def _saver(self):
        while True:
            time.sleep(2)
            with self.lock:
                if not self._dirty:
                    continue
                data = json.dumps(self.doc, separators=(",", ":"))
                self._dirty = False
            tmp = self.store / "session.json.tmp"
            tmp.write_text(data)
            tmp.replace(self.store / "session.json")

    def _index(self):
        self.pages = {p["id"]: p for p in self.doc["pages"]}
        self.strokes = {s["id"]: s for p in self.doc["pages"] for s in p["strokes"]}

    def touch(self):
        self._dirty = True

    # connections
    def peers(self):
        return {"pads": sum(c.role == "pad" for c in self.conns),
                "views": sum(c.role == "view" for c in self.conns)}

    def join(self, conn):
        with self.lock:
            self.conns.append(conn)
            conn.send({"t": "state", "doc": self.doc, "you": conn.id, **self.peers()})
            self.broadcast({"t": "peers", **self.peers()})

    def leave(self, conn):
        with self.lock:
            if conn in self.conns:
                self.conns.remove(conn)
            self.broadcast({"t": "peers", **self.peers()})
            if conn.role == "pad":
                self.broadcast({"t": "cursor", "hide": True, "from": conn.id})
        conn.close()

    def broadcast(self, msg, skip=None):
        for c in list(self.conns):
            if c is not skip:
                c.send(msg)

    def send_state(self):
        with self.lock:
            msg = {"t": "state", "doc": self.doc, **self.peers()}
            for c in list(self.conns):
                c.send({**msg, "you": c.id})

    # messages
    def handle(self, conn, msg):
        t = msg.get("t")
        with self.lock:
            page = self.pages.get(msg.get("page"))
            if t == "begin" and page:
                s = msg.get("s") or {}
                if not isinstance(s.get("id"), str) or s["id"] in self.strokes:
                    return
                s.setdefault("pts", [])
                page["strokes"].append(s)
                self.strokes[s["id"]] = s
                self.touch()
            elif t == "pts":
                s = self.strokes.get(msg.get("id"))
                if not s:
                    return
                s["pts"].extend(msg.get("pts") or [])
                self.touch()
            elif t == "end":
                if msg.get("id") not in self.strokes:
                    return
            elif t == "remove" and page:
                ids = set(msg.get("ids") or [])
                page["strokes"] = [s for s in page["strokes"] if s["id"] not in ids]
                for i in ids:
                    self.strokes.pop(i, None)
                self.touch()
            elif t == "restore" and page:
                for s in msg.get("strokes") or []:
                    if s.get("id") in self.strokes:
                        continue
                    page["strokes"].append(s)
                    self.strokes[s["id"]] = s
                page["strokes"].sort(key=lambda s: s.get("z", 0))
                self.touch()
            elif t == "goto":
                i = msg.get("index")
                if isinstance(i, int) and 0 <= i < len(self.doc["pages"]):
                    self.doc["page"] = i
                    self.touch()
            elif t == "paper" and page:
                page["paper"] = str(msg.get("paper", "blank"))
                self.touch()
            elif t == "addpage":
                i = self.doc["page"]
                cur = self.doc["pages"][i]
                p = blank_page(cur["h"] / cur["w"], cur.get("paper", "grid"), cur.get("pt", (960, 540)))
                self.doc["pages"].insert(i + 1, p)
                self.doc["page"] = i + 1
                self._index()
                self.touch()
                self.send_state()
                return
            elif t == "delpage":
                if len(self.doc["pages"]) > 1 and page:
                    self.doc["pages"].remove(page)
                    self.doc["page"] = min(self.doc["page"], len(self.doc["pages"]) - 1)
                    self._index()
                    self.touch()
                    self.send_state()
                return
            elif t == "newdoc":
                self.doc = blank_doc()
                self._index()
                self.touch()
                self.send_state()
                return
            elif t == "cursor":
                pass  # ephemeral, relay only
            else:
                return
            msg["from"] = conn.id
            self.broadcast(msg, skip=conn)

    def load_pdf(self, name, path: Path, sizes):
        with self.lock:
            rev = new_id()
            final = self.store / f"pdf-{rev}.pdf"
            shutil.move(str(path), final)
            for old in self.store.glob("pdf-*"):
                if old.name != final.name:
                    shutil.rmtree(old, ignore_errors=True) if old.is_dir() else old.unlink(missing_ok=True)
            pages = []
            for i, (w, h) in enumerate(sizes):
                p = blank_page(h / w, "blank", (w, h))
                p["bg"] = {"pdf": rev, "n": i + 1}
                pages.append(p)
            self.doc = {"id": new_id(), "pages": pages, "page": 0, "pdf": {"name": name, "rev": rev}}
            self._index()
            self.touch()
        self.send_state()


# --------------------------------------------------------------------------- PDF


def pdf_page_sizes(path: Path):
    out = subprocess.run(["pdfinfo", "-f", "1", "-l", "100000", str(path)],
                         capture_output=True, text=True, timeout=60)
    if out.returncode != 0:
        raise ValueError(out.stderr.strip() or "pdfinfo failed")
    sizes = []
    rot = {}
    for line in out.stdout.splitlines():
        m = re.match(r"Page\s+(\d+)\s+size:\s+([\d.]+)\s+x\s+([\d.]+)", line)
        if m:
            sizes.append((float(m.group(2)), float(m.group(3))))
        m = re.match(r"Page\s+(\d+)\s+rot:\s+(\d+)", line)
        if m:
            rot[int(m.group(1))] = int(m.group(2))
    if not sizes:
        m = re.search(r"Page size:\s+([\d.]+)\s+x\s+([\d.]+)", out.stdout)
        pages = re.search(r"Pages:\s+(\d+)", out.stdout)
        if m and pages:
            sizes = [(float(m.group(1)), float(m.group(2)))] * int(pages.group(1))
    sizes = [(h, w) if rot.get(i + 1, 0) % 180 == 90 else (w, h) for i, (w, h) in enumerate(sizes)]
    if not sizes:
        raise ValueError("could not read PDF pages")
    return sizes


_render_locks: dict[str, threading.Lock] = {}
_render_guard = threading.Lock()


def render_pdf_page(store: Path, rev: str, n: int) -> Path | None:
    pdf = store / f"pdf-{rev}.pdf"
    if not pdf.exists():
        return None
    cache = store / f"pdf-{rev}"
    cache.mkdir(exist_ok=True)
    out = cache / f"{n}.png"
    with _render_guard:
        lock = _render_locks.setdefault(f"{rev}/{n}", threading.Lock())
    with lock:
        if not out.exists():
            subprocess.run(["pdftoppm", "-png", "-singlefile", "-f", str(n), "-l", str(n),
                            "-scale-to", str(PDF_RENDER_PX), str(pdf), str(cache / f"{n}")],
                           capture_output=True, timeout=120)
    return out if out.exists() else None


# --------------------------------------------------------------------------- HTTP


class Handler(BaseHTTPRequestHandler):
    server_version = "inkcast"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        if os.environ.get("INKCAST_DEBUG"):
            super().log_message(fmt, *args)

    @property
    def app(self) -> "InkcastServer":
        return self.server  # type: ignore[return-value]

    def _is_local(self):
        return self.client_address[0] in ("127.0.0.1", "::1", "::ffff:127.0.0.1")

    def _authorized(self, q):
        if self._is_local():
            return True
        k = (q.get("k") or [""])[0] or self.headers.get("X-Inkcast-Key", "")
        return hmac.compare_digest(k, self.app.token)

    def _send(self, code, body=b"", ctype="text/plain; charset=utf-8", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj).encode(), "application/json")

    def _file(self, path: Path, cache=False):
        try:
            body = path.read_bytes()
        except OSError:
            return self._send(404, b"not found")
        extra = {"Cache-Control": "public, max-age=31536000, immutable"} if cache else None
        self.send_response(200)
        self.send_header("Content-Type", MIME.get(path.suffix, "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", (extra or {}).get("Cache-Control", "no-store"))
        self.end_headers()
        self.wfile.write(body)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        url = urlparse(self.path)
        q = parse_qs(url.query)
        path = url.path

        if path == "/api/ping":
            return self._json({"app": "inkcast", "pid": os.getpid()})
        if path in ("/", "/pad"):
            if not self._authorized(q):
                return self._send(403, b"Inkcast: missing or wrong key. Scan the QR code shown in the viewer window.")
            return self._file(WEB_DIR / "pad.html")
        if path == "/view":
            if not self._authorized(q):
                return self._send(403, b"forbidden")
            return self._file(WEB_DIR / "view.html")
        if path == "/ws":
            if not self._authorized(q):
                return self._send(403, b"forbidden")
            return self._websocket((q.get("role") or ["view"])[0])
        if path == "/api/info":
            if not self._is_local():
                return self._send(403, b"forbidden")
            return self._json({"urls": self.app.pad_urls(), "token": self.app.token})
        if path == "/api/qr.svg":
            if not self._is_local():
                return self._send(403, b"forbidden")
            return self._qr()
        m = re.fullmatch(r"/api/pdf/([0-9a-f]+)/(\d+)\.png", path)
        if m:
            if not self._authorized(q):
                return self._send(403, b"forbidden")
            out = render_pdf_page(self.app.store, m.group(1), int(m.group(2)))
            return self._file(out, cache=True) if out else self._send(404, b"not found")
        static = (WEB_DIR / path.lstrip("/")).resolve()
        if static.is_file() and WEB_DIR in static.parents:
            return self._file(static)
        self._send(404, b"not found")

    def do_POST(self):
        url = urlparse(self.path)
        q = parse_qs(url.query)
        if url.path == "/api/shutdown" and self._is_local():
            self._json({"ok": True})
            threading.Thread(target=self.app.shutdown, daemon=True).start()
            return
        if url.path == "/api/pdf":
            if not self._authorized(q):
                return self._send(403, b"forbidden")
            return self._upload_pdf((q.get("name") or ["document.pdf"])[0])
        self._send(404, b"not found")

    def _upload_pdf(self, name):
        if not shutil.which("pdftoppm") or not shutil.which("pdfinfo"):
            return self._json({"error": "poppler (pdftoppm/pdfinfo) is not installed on the computer"}, 500)
        try:
            n = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            n = 0
        if n <= 0 or n > MAX_PDF_BYTES:
            return self._json({"error": "PDF is empty or too large"}, 413)
        tmp = self.app.store / f"upload-{new_id()}.pdf"
        with open(tmp, "wb") as f:
            left = n
            while left:
                chunk = self.rfile.read(min(left, 1 << 20))
                if not chunk:
                    break
                f.write(chunk)
                left -= len(chunk)
        try:
            sizes = pdf_page_sizes(tmp)
        except (ValueError, subprocess.SubprocessError) as e:
            tmp.unlink(missing_ok=True)
            return self._json({"error": f"Not a readable PDF: {e}"}, 400)
        self.app.hub.load_pdf(name, tmp, sizes)
        self._json({"ok": True, "pages": len(sizes)})

    def _qr(self):
        url = (self.app.pad_urls() or [""])[0]
        if not url or not shutil.which("qrencode"):
            return self._send(404, b"no qrencode")
        out = subprocess.run(["qrencode", "-t", "SVG", "-m", "1", "--rle", "-o", "-", url],
                             capture_output=True, timeout=10)
        self._send(200, out.stdout, "image/svg+xml")

    def _websocket(self, role):
        key = self.headers.get("Sec-WebSocket-Key")
        if not key or "websocket" not in self.headers.get("Upgrade", "").lower():
            return self._send(400, b"expected websocket")
        accept = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
        self.send_response(HTTPStatus.SWITCHING_PROTOCOLS)
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.end_headers()
        self.wfile.flush()
        self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        conn = WSConn(self.connection, self.rfile, "pad" if role == "pad" else "view", self.client_address[0])
        hub = self.app.hub
        hub.join(conn)
        try:
            while conn.alive:
                raw = conn.recv()
                if raw is None:
                    break
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(msg, dict):
                    hub.handle(conn, msg)
        except (ConnectionError, OSError):
            pass
        finally:
            hub.leave(conn)
            self.close_connection = True


# --------------------------------------------------------------------------- Network helpers

SKIP_IFACES = re.compile(r"^(lo|tun|tap|wg|proton|tailscale|docker|br-|veth|virbr|utun|llw|awdl|bridge|zt)")


def lan_addresses():
    """Private IPv4 addresses of physical-looking interfaces, best first."""
    found = []
    try:
        out = subprocess.run(["ip", "-j", "-4", "addr"], capture_output=True, text=True, timeout=5)
        for iface in json.loads(out.stdout or "[]"):
            name = iface.get("ifname", "")
            for a in iface.get("addr_info", []):
                found.append((name, a.get("local", "")))
    except (OSError, ValueError, subprocess.SubprocessError):
        try:  # macOS / BSD
            out = subprocess.run(["ifconfig"], capture_output=True, text=True, timeout=5).stdout
            name = ""
            for line in out.splitlines():
                if line and not line[0].isspace():
                    name = line.split(":")[0]
                m = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", line)
                if m:
                    found.append((name, m.group(1)))
        except (OSError, subprocess.SubprocessError):
            pass

    def private(ip):
        a, b = (int(x) for x in ip.split(".")[:2])
        return a == 10 or (a == 172 and 16 <= b <= 31) or (a == 192 and b == 168)

    good = [(n, ip) for n, ip in found if ip and private(ip) and not SKIP_IFACES.match(n)]
    good.sort(key=lambda x: (not x[1].startswith("192.168."), not re.match(r"^(wl|en|eth)", x[0])))
    return [ip for _, ip in good]


class InkcastServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, host, port, public_host=None):
        super().__init__((host, port), Handler)
        self.store = data_dir()
        self.port = port
        self.public_host = public_host
        tok = self.store / "token"
        if not tok.exists():
            tok.write_text(secrets.token_urlsafe(9))
            tok.chmod(0o600)
        self.token = tok.read_text().strip()
        self.hub = Hub(self.store)

    def pad_urls(self):
        hosts = [self.public_host] if self.public_host else lan_addresses()
        return [f"http://{h}:{self.port}/?k={self.token}" for h in hosts]
