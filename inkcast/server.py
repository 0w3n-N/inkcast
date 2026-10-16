"""Inkcast server: a tiny LAN whiteboard hub.

Standard library only. Serves the iPad pad UI and the desktop viewer, relays
pen strokes over WebSockets, keeps the document in memory (persisted to disk),
and rasterizes PDF pages with poppler's `pdftoppm`.

Security model (see SECURITY.md):
  * Every data endpoint needs the secret key (128-bit, in an owner-only file).
    There is no "trusted localhost": other local users and web pages loaded in
    the desktop browser can reach 127.0.0.1 too.
  * The Host header must name this machine (defeats DNS rebinding) and any
    browser Origin must be this server (defeats cross-site WebSockets/CSRF).
  * Everything a client sends is validated and rebuilt before it is stored or
    relayed, and sizes are capped.
"""

import base64
import hashlib
import hmac
import ipaddress
import json
import os
import queue
import re
import secrets
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import __version__
from .theme import Theme

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
PAGE_W = 1000  # page coordinates: width is always 1000 units

# Limits. Generous for real use, small enough that a misbehaving client can't
# exhaust memory, disk, or threads.
MAX_PDF_BYTES = 200 * 1024 * 1024
MAX_WS_MESSAGE = 2 * 1024 * 1024
MAX_PAGES = 1000
MAX_STROKES_PER_PAGE = 20000
MAX_POINTS_PER_STROKE = 50000
MAX_IDS_PER_MESSAGE = 20000
MAX_WS_CLIENTS = 24
MAX_HTTP_CONNECTIONS = 64
HTTP_TIMEOUT = 20
WS_TIMEOUT = 90
WS_PING_EVERY = 25
PDF_RENDER_PX = 2000
LAUNCH_CODE_TTL = 60

PAPERS = ("blank", "grid", "lines", "dots")
TOOLS = ("pen", "hl")
ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
REV_RE = re.compile(r"^[0-9a-f]{12}$")
COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")

# The only files served from web/. An allowlist instead of path resolution.
STATIC = {
    "/pad.js": ("pad.js", "text/javascript; charset=utf-8"),
    "/view.js": ("view.js", "text/javascript; charset=utf-8"),
    "/ink.js": ("ink.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
    "/icon.svg": ("icon.svg", "image/svg+xml"),
    "/icon.png": ("icon.png", "image/png"),
}
PAGES = {"/": "pad.html", "/pad": "pad.html", "/view": "view.html"}


# --------------------------------------------------------------------------- paths


def data_dir() -> Path:
    if sys.platform == "darwin" and "XDG_DATA_HOME" not in os.environ:
        base = os.path.expanduser("~/Library/Application Support")
    else:
        base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return private_dir(Path(base) / "inkcast")


def runtime_dir() -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR")
    if base and os.path.isdir(base):
        return private_dir(Path(base) / "inkcast")
    return private_dir(data_dir() / "run")


def private_dir(d: Path) -> Path:
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(d, 0o700)
    return d


def write_private(path: Path, data: str | bytes):
    """Atomically write a file only the owner can read."""
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data.encode() if isinstance(data, str) else data)
    os.replace(tmp, path)


def load_token(store: Path) -> str:
    tok = store / "token"
    try:
        value = tok.read_text().strip()
        if re.fullmatch(r"[A-Za-z0-9_-]{22,64}", value):
            os.chmod(tok, 0o600)
            return value
    except OSError:
        pass
    value = secrets.token_urlsafe(16)  # 128 bits
    write_private(tok, value)
    return value


def new_id() -> str:
    return secrets.token_hex(6)


# --------------------------------------------------------------------------- validation


def num(v, lo, hi):
    """A finite number clamped to [lo, hi], or None."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    if v != v or v in (float("inf"), float("-inf")):
        return None
    return min(hi, max(lo, v))


def clean_point(p):
    if not isinstance(p, (list, tuple)) or len(p) < 2:
        return None
    x, y = num(p[0], -1e5, 1e5), num(p[1], -1e5, 1e5)
    if x is None or y is None:
        return None
    pr = num(p[2], 0, 1) if len(p) > 2 else None
    return [round(x, 2), round(y, 2), round(0.5 if pr is None else pr, 3)]


def clean_points(pts, room):
    if not isinstance(pts, list) or room <= 0:
        return []
    out = []
    for p in pts[:room]:
        c = clean_point(p)
        if c:
            out.append(c)
    return out


def clean_id(v):
    return v if isinstance(v, str) and ID_RE.match(v) else None


def clean_stroke(s):
    if not isinstance(s, dict) or not clean_id(s.get("id")):
        return None
    color = s.get("color")
    return {
        "id": s["id"],
        "z": num(s.get("z"), 0, 1e17) or 0,
        "tool": s.get("tool") if s.get("tool") in TOOLS else "pen",
        "color": color if isinstance(color, str) and COLOR_RE.match(color) else "#111111",
        "width": num(s.get("width"), 0.5, 80) or 3.5,
        "pts": clean_points(s.get("pts"), MAX_POINTS_PER_STROKE),
    }


def blank_page(aspect=9 / 16, paper="grid", pt=(960, 540)):
    return {"id": new_id(), "w": PAGE_W, "h": round(PAGE_W * aspect, 2), "paper": paper,
            "bg": None, "pt": [round(pt[0], 2), round(pt[1], 2)], "strokes": []}


def blank_doc():
    return {"id": new_id(), "pages": [blank_page()], "page": 0, "pdf": None}


def clean_page(p):
    if not isinstance(p, dict) or not clean_id(p.get("id")):
        return None
    h = num(p.get("h"), 10, 100000)
    if h is None:
        return None
    bg = p.get("bg")
    if isinstance(bg, dict) and isinstance(bg.get("pdf"), str) and REV_RE.match(bg["pdf"]) \
            and isinstance(bg.get("n"), int) and not isinstance(bg.get("n"), bool) and 1 <= bg["n"] <= 100000:
        bg = {"pdf": bg["pdf"], "n": bg["n"]}
    else:
        bg = None
    pt = p.get("pt") if isinstance(p.get("pt"), list) and len(p.get("pt")) == 2 else [960, 540]
    pt = [num(pt[0], 1, 1e5) or 960, num(pt[1], 1, 1e5) or 540]
    strokes = []
    seen = set()
    for s in (p.get("strokes") if isinstance(p.get("strokes"), list) else [])[:MAX_STROKES_PER_PAGE]:
        c = clean_stroke(s)
        if c and c["id"] not in seen:
            seen.add(c["id"])
            strokes.append(c)
    return {"id": p["id"], "w": PAGE_W, "h": h, "paper": p.get("paper") if p.get("paper") in PAPERS else "blank",
            "bg": bg, "pt": pt, "strokes": strokes}


def clean_doc(doc):
    if not isinstance(doc, dict) or not isinstance(doc.get("pages"), list):
        return None
    pages = [c for c in (clean_page(p) for p in doc["pages"][:MAX_PAGES]) if c]
    if not pages or len({p["id"] for p in pages}) != len(pages):
        return None
    pdf = doc.get("pdf")
    if isinstance(pdf, dict) and isinstance(pdf.get("rev"), str) and REV_RE.match(pdf["rev"]):
        pdf = {"name": clean_name(pdf.get("name")), "rev": pdf["rev"]}
    else:
        pdf = None
    page = doc.get("page") if isinstance(doc.get("page"), int) else 0
    return {"id": clean_id(doc.get("id")) or new_id(), "pages": pages,
            "page": min(max(0, page), len(pages) - 1), "pdf": pdf}


def clean_name(v):
    name = re.sub(r"[\x00-\x1f\x7f/\\]", "", str(v or "document.pdf")).strip()
    return (name or "document.pdf")[:120]


# --------------------------------------------------------------------------- WebSocket


class WSConn:
    def __init__(self, sock, rfile, role):
        self.sock = sock
        self.rfile = rfile
        self.role = role
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

    def ping(self):
        if self.alive:
            self.out.put(self._frame(0x9, b""))

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
        size = 0
        while True:
            b1, b2 = self._read(2)
            fin, opcode = b1 & 0x80, b1 & 0x0F
            if not b2 & 0x80:
                raise ConnectionError("client frames must be masked")
            n = b2 & 0x7F
            if n == 126:
                n = struct.unpack("!H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack("!Q", self._read(8))[0]
            size += n
            if size > MAX_WS_MESSAGE:
                raise ConnectionError("message too large")
            mask = self._read(4)
            data = self._read(n) if n else b""
            if n:
                key = (mask * (n // 4 + 1))[:n]
                data = (int.from_bytes(data, "big") ^ int.from_bytes(key, "big")).to_bytes(n, "big")
            if opcode == 0x8:
                return None
            if opcode == 0x9:
                if n <= 125:
                    self.out.put(self._frame(0xA, data))
                size -= n
                continue
            if opcode == 0xA:
                size -= n
                continue
            if opcode not in (0x0, 0x1):
                raise ConnectionError("unsupported frame")
            parts.append(data)
            if fin:
                return b"".join(parts).decode("utf-8", "replace")


# --------------------------------------------------------------------------- Document hub


class Hub:
    def __init__(self, store: Path, on_change=None):
        self.store = store
        self.lock = threading.RLock()
        self.conns: list[WSConn] = []
        self.on_change = on_change or (lambda: None)
        self.doc = self._load()
        self._index()
        self._dirty = False

    # persistence
    def _load(self):
        try:
            doc = clean_doc(json.loads((self.store / "session.json").read_text()))
            if doc:
                return doc
        except (OSError, ValueError, RecursionError):
            pass
        return blank_doc()

    def save_if_dirty(self):
        with self.lock:
            if not self._dirty:
                return
            data = json.dumps(self.doc, separators=(",", ":"))
            self._dirty = False
        write_private(self.store / "session.json", data)

    def _index(self):
        self.pages = {p["id"]: p for p in self.doc["pages"]}
        self.strokes = {s["id"]: s for p in self.doc["pages"] for s in p["strokes"]}

    def touch(self):
        self._dirty = True

    # connections
    def peers(self):
        return {"pads": sum(c.role == "pad" for c in self.conns),
                "views": sum(c.role == "view" for c in self.conns)}

    def join(self, conn) -> bool:
        with self.lock:
            if len(self.conns) >= MAX_WS_CLIENTS:
                return False
            self.conns.append(conn)
            conn.send({"t": "state", "doc": self.doc, "you": conn.id, **self.peers()})
            self.broadcast({"t": "peers", **self.peers()})
        self.on_change()
        return True

    def leave(self, conn):
        with self.lock:
            if conn in self.conns:
                self.conns.remove(conn)
            self.broadcast({"t": "peers", **self.peers()})
            if conn.role == "pad":
                self.broadcast({"t": "cursor", "hide": True, "from": conn.id})
        conn.close()
        self.on_change()

    def broadcast(self, msg, skip=None):
        for c in list(self.conns):
            if c is not skip:
                c.send(msg)

    def ping_all(self):
        for c in list(self.conns):
            c.ping()

    def send_state(self):
        with self.lock:
            msg = {"t": "state", "doc": self.doc, **self.peers()}
            for c in list(self.conns):
                c.send({**msg, "you": c.id})
        self.on_change()

    # messages: every relayed message is rebuilt from validated fields
    def handle(self, conn, msg):
        t = msg.get("t")
        with self.lock:
            page = self.pages.get(msg.get("page")) if isinstance(msg.get("page"), str) else None
            out = None
            if t == "begin" and page:
                s = clean_stroke(msg.get("s"))
                if not s or s["id"] in self.strokes or len(page["strokes"]) >= MAX_STROKES_PER_PAGE:
                    return
                page["strokes"].append(s)
                self.strokes[s["id"]] = s
                out = {"t": "begin", "page": page["id"], "s": s}
            elif t == "pts":
                s = self.strokes.get(clean_id(msg.get("id")))
                pts = clean_points(msg.get("pts"), MAX_POINTS_PER_STROKE - len(s["pts"])) if s else []
                if not pts:
                    return
                s["pts"].extend(pts)
                out = {"t": "pts", "id": s["id"], "pts": pts}
            elif t == "end":
                sid = clean_id(msg.get("id"))
                if sid not in self.strokes:
                    return
                out = {"t": "end", "id": sid}
            elif t == "remove" and page:
                ids = msg.get("ids") if isinstance(msg.get("ids"), list) else []
                ids = {i for i in ids[:MAX_IDS_PER_MESSAGE] if clean_id(i)}
                gone = [s["id"] for s in page["strokes"] if s["id"] in ids]
                if not gone:
                    return
                page["strokes"] = [s for s in page["strokes"] if s["id"] not in ids]
                for i in gone:
                    self.strokes.pop(i, None)
                out = {"t": "remove", "page": page["id"], "ids": gone}
            elif t == "restore" and page:
                strokes = msg.get("strokes") if isinstance(msg.get("strokes"), list) else []
                added = []
                for raw in strokes[:MAX_IDS_PER_MESSAGE]:
                    s = clean_stroke(raw)
                    if not s or s["id"] in self.strokes or len(page["strokes"]) >= MAX_STROKES_PER_PAGE:
                        continue
                    page["strokes"].append(s)
                    self.strokes[s["id"]] = s
                    added.append(s)
                if not added:
                    return
                page["strokes"].sort(key=lambda s: s["z"])
                out = {"t": "restore", "page": page["id"], "strokes": added}
            elif t == "goto":
                i = msg.get("index")
                if isinstance(i, bool) or not isinstance(i, int) or not 0 <= i < len(self.doc["pages"]):
                    return
                self.doc["page"] = i
                out = {"t": "goto", "index": i}
            elif t == "paper" and page:
                if msg.get("paper") not in PAPERS:
                    return
                page["paper"] = msg["paper"]
                out = {"t": "paper", "page": page["id"], "paper": page["paper"]}
            elif t == "cursor":
                x, y = num(msg.get("x"), -1e5, 1e5), num(msg.get("y"), -1e5, 1e5)
                if msg.get("hide") is True or not page or x is None or y is None:
                    out = {"t": "cursor", "hide": True}
                else:
                    out = {"t": "cursor", "page": page["id"], "x": round(x, 1), "y": round(y, 1)}
                out["from"] = conn.id
                self.broadcast(out, skip=conn)
                return  # ephemeral: not stored
            elif t == "addpage":
                if len(self.doc["pages"]) >= MAX_PAGES:
                    return
                i = self.doc["page"]
                cur = self.doc["pages"][i]
                self.doc["pages"].insert(i + 1, blank_page(cur["h"] / cur["w"], cur["paper"] if not cur["bg"] else "grid", cur["pt"]))
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
            if out is None:
                return
            self.touch()
            out["from"] = conn.id
            self.broadcast(out, skip=conn)
        if t in ("goto",):
            self.on_change()

    def load_pdf(self, name, path: Path, sizes):
        with self.lock:
            rev = new_id()
            final = self.store / f"pdf-{rev}.pdf"
            os.replace(path, final)
            for old in self.store.glob("pdf-*"):
                if old.name != final.name:
                    shutil.rmtree(old, ignore_errors=True) if old.is_dir() else old.unlink(missing_ok=True)
            pages = []
            for i, (w, h) in enumerate(sizes[:MAX_PAGES]):
                p = blank_page(h / w, "blank", (w, h))
                p["bg"] = {"pdf": rev, "n": i + 1}
                pages.append(p)
            self.doc = {"id": new_id(), "pages": pages, "page": 0, "pdf": {"name": clean_name(name), "rev": rev}}
            self._index()
            self.touch()
        self.send_state()

    def pdf_page_ok(self, rev, n):
        with self.lock:
            pdf = self.doc.get("pdf")
            return bool(pdf) and pdf["rev"] == rev and any(p["bg"] and p["bg"]["n"] == n for p in self.doc["pages"])


# --------------------------------------------------------------------------- PDF

_render_slots = threading.BoundedSemaphore(2)
_render_locks: dict[str, threading.Lock] = {}
_render_guard = threading.Lock()


def limited(cmd):
    """Run untrusted-input parsers (poppler) under CPU and memory limits."""
    if shutil.which("prlimit"):
        return ["prlimit", "--as=3221225472", "--cpu=120", "--nofile=256", "--"] + cmd
    return cmd


def pdf_page_sizes(path: Path):
    out = subprocess.run(limited(["pdfinfo", "-f", "1", "-l", str(MAX_PAGES), str(path)]),
                         capture_output=True, text=True, timeout=60, errors="replace")
    if out.returncode != 0:
        raise ValueError((out.stderr.strip().splitlines() or ["pdfinfo failed"])[-1][:200])
    sizes, rot = [], {}
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
            sizes = [(float(m.group(1)), float(m.group(2)))] * min(int(pages.group(1)), MAX_PAGES)
    sizes = [(h, w) if rot.get(i + 1, 0) % 180 == 90 else (w, h) for i, (w, h) in enumerate(sizes)]
    sizes = [(w, h) for w, h in sizes if w > 0 and h > 0]
    if not sizes:
        raise ValueError("could not read PDF pages")
    return sizes


def render_pdf_page(store: Path, rev: str, n: int) -> Path | None:
    pdf = store / f"pdf-{rev}.pdf"
    if not pdf.exists():
        return None
    cache = store / f"pdf-{rev}"
    cache.mkdir(exist_ok=True, mode=0o700)
    out = cache / f"{n}.png"
    with _render_guard:
        lock = _render_locks.setdefault(f"{rev}/{n}", threading.Lock())
    with lock:
        if not out.exists():
            with _render_slots:
                try:
                    subprocess.run(limited(["pdftoppm", "-png", "-singlefile", "-f", str(n), "-l", str(n),
                                            "-scale-to", str(PDF_RENDER_PX), str(pdf), str(cache / f"{n}")]),
                                   capture_output=True, timeout=120)
                except subprocess.TimeoutExpired:
                    return None
    return out if out.exists() else None


# --------------------------------------------------------------------------- HTTP

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Cross-Origin-Resource-Policy": "same-origin",
}


class Handler(BaseHTTPRequestHandler):
    server_version = "inkcast"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    timeout = HTTP_TIMEOUT

    def log_message(self, fmt, *args):
        if os.environ.get("INKCAST_DEBUG"):
            # Never log query strings: they carry the key.
            sys.stderr.write(f"{self.client_address[0]} {self.command} {urlparse(self.path).path}\n")

    @property
    def app(self) -> "InkcastServer":
        return self.server  # type: ignore[return-value]

    # ------------------------------------------------------------- checks

    def _host_ok(self):
        return self.headers.get("Host", "").strip().lower() in self.app.allowed_hosts()

    def _origin_ok(self):
        origin = self.headers.get("Origin")
        if origin is None:
            return True  # not a browser (curl, the CLI); the key is still required
        return origin.strip().lower() in {f"http://{h}" for h in self.app.allowed_hosts()}

    def _key_ok(self, q, header_only=False):
        cand = self.headers.get("X-Inkcast-Key", "")
        if not cand and not header_only:
            cand = (q.get("k") or [""])[0]
        return bool(cand) and hmac.compare_digest(cand.encode(), self.app.token.encode())

    # ------------------------------------------------------------- responses

    def _send(self, code, body=b"", ctype="text/plain; charset=utf-8", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        headers = {"Cache-Control": "no-store", **SECURITY_HEADERS, **(extra or {})}
        for k, v in headers.items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj).encode(), "application/json")

    def _deny(self, code=403, msg="forbidden"):
        self._json({"error": msg}, code)

    def _page(self, name):
        host = self.headers.get("Host", "").strip().lower()
        csp = ("default-src 'none'; script-src 'self'; style-src 'self'; "
               "img-src 'self' blob: data:; "
               f"connect-src 'self' ws://{host}; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
        self._send(200, (WEB_DIR / name).read_bytes(), "text/html; charset=utf-8",
                   {"Content-Security-Policy": csp})

    # ------------------------------------------------------------- routing

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        if not self._host_ok():
            return self._deny(421, "unknown host")
        url = urlparse(self.path)
        q = parse_qs(url.query)
        path = url.path

        if path == "/api/ping":
            return self._json({"app": "inkcast", "version": __version__})
        if path in PAGES:
            return self._page(PAGES[path])
        if path in STATIC:
            name, ctype = STATIC[path]
            return self._send(200, (WEB_DIR / name).read_bytes(), ctype)

        # everything below needs the key
        if not self._key_ok(q):
            return self._deny()
        if path == "/ws":
            if not self._origin_ok():
                return self._deny()
            return self._websocket((q.get("role") or ["view"])[0])
        if path == "/theme.css":
            return self._send(200, self.app.theme.css().encode(), "text/css; charset=utf-8")
        if path == "/api/info":
            return self._json({"urls": self.app.pad_urls(), "version": __version__})
        if path == "/api/qr.svg":
            return self._qr()
        m = re.fullmatch(r"/api/pdf/([0-9a-f]{12})/(\d{1,6})\.png", path)
        if m and self.app.hub.pdf_page_ok(m.group(1), int(m.group(2))):
            out = render_pdf_page(self.app.store, m.group(1), int(m.group(2)))
            if out:
                return self._send(200, out.read_bytes(), "image/png",
                                  {"Cache-Control": "private, max-age=31536000, immutable"})
        self._deny(404, "not found")

    def do_POST(self):
        if not self._host_ok():
            return self._deny(421, "unknown host")
        if not self._origin_ok():
            return self._deny()
        url = urlparse(self.path)
        q = parse_qs(url.query)

        if url.path == "/api/redeem":
            # One-time launch code -> key. Lets the CLI open the viewer without
            # putting the long-lived key on a process command line.
            body = self._read_small_body()
            code = body.get("c") if isinstance(body, dict) else None
            key = self.app.redeem(code)
            return self._json({"k": key}) if key else self._deny()

        # The CLI's control endpoints accept the key only as a header.
        if url.path == "/api/launch":
            if not self._key_ok(q, header_only=True):
                return self._deny()
            return self._json({"c": self.app.new_launch_code()})
        if url.path == "/api/shutdown":
            if not self._key_ok(q, header_only=True):
                return self._deny()
            self._json({"ok": True})
            threading.Thread(target=self.app.shutdown, daemon=True).start()
            return
        if url.path == "/api/pdf":
            if not self._key_ok(q):
                return self._deny()
            return self._upload_pdf((q.get("name") or ["document.pdf"])[0])
        self._deny(404, "not found")

    def _read_small_body(self):
        try:
            n = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return None
        if not 0 < n <= 4096:
            return None
        try:
            return json.loads(self.rfile.read(n))
        except ValueError:
            return None

    def _upload_pdf(self, name):
        if not shutil.which("pdftoppm") or not shutil.which("pdfinfo"):
            return self._json({"error": "poppler (pdftoppm/pdfinfo) is not installed on the computer"}, 500)
        try:
            n = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            n = 0
        if n <= 0 or n > MAX_PDF_BYTES:
            self.close_connection = True
            return self._json({"error": f"PDF is empty or larger than {MAX_PDF_BYTES // 1048576} MB"}, 413)
        tmp = self.app.store / f"upload-{new_id()}.pdf"
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as f:
                left = n
                first = True
                while left:
                    chunk = self.rfile.read(min(left, 1 << 20))
                    if not chunk:
                        raise ValueError("upload interrupted")
                    if first and not chunk.startswith(b"%PDF-"):
                        raise ValueError("that file is not a PDF")
                    first = False
                    f.write(chunk)
                    left -= len(chunk)
            sizes = pdf_page_sizes(tmp)
        except (ValueError, OSError, subprocess.SubprocessError) as e:
            tmp.unlink(missing_ok=True)
            self.close_connection = True
            return self._json({"error": f"Could not open PDF: {e}"}, 400)
        self.app.hub.load_pdf(name, tmp, sizes)
        self._json({"ok": True, "pages": len(sizes)})

    def _qr(self):
        url = (self.app.pad_urls() or [""])[0]
        if not url or not shutil.which("qrencode"):
            return self._deny(404, "qrencode is not installed")
        out = subprocess.run(["qrencode", "-t", "SVG", "-m", "1", "--rle", "-o", "-", url],
                             capture_output=True, timeout=10)
        self._send(200, out.stdout, "image/svg+xml")

    def _websocket(self, role):
        key = self.headers.get("Sec-WebSocket-Key")
        if not key or "websocket" not in self.headers.get("Upgrade", "").lower():
            return self._deny(400, "expected websocket")
        accept = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
        self.send_response(HTTPStatus.SWITCHING_PROTOCOLS)
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.end_headers()
        self.wfile.flush()
        self.close_connection = True
        self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.connection.settimeout(WS_TIMEOUT)  # server pings keep live peers inside this
        conn = WSConn(self.connection, self.rfile, "pad" if role == "pad" else "view")
        hub = self.app.hub
        if not hub.join(conn):
            conn.close()
            return
        try:
            while conn.alive:
                raw = conn.recv()
                if raw is None:
                    break
                try:
                    msg = json.loads(raw)
                except (ValueError, RecursionError):
                    continue
                if isinstance(msg, dict):
                    hub.handle(conn, msg)
        except (ConnectionError, OSError):
            pass
        finally:
            hub.leave(conn)


# --------------------------------------------------------------------------- Network helpers

SKIP_IFACES = re.compile(r"^(lo|tun|tap|wg|proton|tailscale|docker|br-|veth|virbr|utun|llw|awdl|bridge|zt|anpi|ap\d)")


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
        try:
            a = ipaddress.ip_address(ip)
        except ValueError:
            return False
        return a.version == 4 and a.is_private and not a.is_loopback and not a.is_link_local

    good = [(n, ip) for n, ip in found if private(ip) and not SKIP_IFACES.match(n)]
    good.sort(key=lambda x: (not x[1].startswith("192.168."), not re.match(r"^(wl|en|eth)", x[0])))
    return [ip for _, ip in good]


def qr_matrix(text):
    """QR code as rows of '0'/'1' (quiet zone included), for the bar plugin."""
    if not shutil.which("qrencode"):
        return []
    try:
        out = subprocess.run(["qrencode", "-t", "ASCII", "-m", "2", "-o", "-", text],
                             capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    rows = []
    for line in out.splitlines():
        if line:
            rows.append("".join("1" if line[i] == "#" else "0" for i in range(0, len(line) - 1, 2)))
    return rows


class InkcastServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 16

    def __init__(self, host, port, public_host=None):
        self.store = data_dir()
        self.port = port
        self.public_host = public_host
        self.token = load_token(self.store)
        self.theme = Theme()
        self._active = 0
        self._active_lock = threading.Lock()
        self._codes: dict[str, float] = {}
        self._hosts: set[str] = set()
        self._hosts_at = 0.0
        self._status_dirty = True
        self.hub = Hub(self.store, on_change=self._mark_status)
        super().__init__((host, port), Handler)
        threading.Thread(target=self._maintenance, daemon=True).start()

    # cap concurrent connections so idle sockets can't exhaust threads
    def process_request(self, request, client_address):
        with self._active_lock:
            if self._active >= MAX_HTTP_CONNECTIONS:
                self.shutdown_request(request)
                return
            self._active += 1
        super().process_request(request, client_address)

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            with self._active_lock:
                self._active -= 1

    def allowed_hosts(self) -> set[str]:
        now = time.monotonic()
        if now - self._hosts_at > 15:
            names = ["127.0.0.1", "localhost", "[::1]", *lan_addresses()]
            if self.public_host:
                names.append(self.public_host)
            self._hosts = {f"{n}:{self.port}".lower() for n in names}
            self._hosts_at = now
        return self._hosts

    def pad_urls(self):
        hosts = [self.public_host] if self.public_host else lan_addresses()
        return [f"http://{h}:{self.port}/?k={self.token}" for h in hosts]

    # one-time launch codes for opening the viewer
    def new_launch_code(self):
        code = secrets.token_urlsafe(16)
        with self._active_lock:
            self._codes[code] = time.monotonic() + LAUNCH_CODE_TTL
        return code

    def redeem(self, code):
        if not isinstance(code, str):
            return None
        with self._active_lock:
            expires = self._codes.pop(code, None)
        return self.token if expires and expires > time.monotonic() else None

    # status file for the Omarchy bar plugin
    def _mark_status(self):
        self._status_dirty = True

    def write_status(self, running=True):
        peers = self.hub.peers()
        url = (self.pad_urls() or [""])[0]
        with self.hub.lock:
            page, pages = self.hub.doc["page"], len(self.hub.doc["pages"])
        status = {"app": "inkcast", "version": __version__, "running": running, "pid": os.getpid(),
                  "port": self.port, "url": url, "qr": qr_matrix(url) if url and running else [],
                  "pads": peers["pads"] if running else 0, "views": peers["views"] if running else 0,
                  "page": page, "pages": pages, "updated": int(time.time())}
        try:
            write_private(runtime_dir() / "status.json", json.dumps(status))
        except OSError:
            pass

    def _maintenance(self):
        tick = 0
        last_url = None
        while True:
            time.sleep(1)
            tick += 1
            try:
                if tick % 2 == 0:
                    self.hub.save_if_dirty()
                    if self.theme.changed():
                        self.hub.broadcast({"t": "theme", "v": self.theme.version})
                if tick % WS_PING_EVERY == 0:
                    self.hub.ping_all()
                if tick % 10 == 0:
                    url = (self.pad_urls() or [""])[0]
                    if url != last_url:
                        last_url = url
                        self._status_dirty = True
                    with self._active_lock:
                        now = time.monotonic()
                        self._codes = {c: e for c, e in self._codes.items() if e > now}
                if self._status_dirty or tick % 10 == 0:
                    self._status_dirty = False
                    self.write_status()
            except Exception as e:  # keep the server alive; report and carry on
                sys.stderr.write(f"inkcast: maintenance error: {e}\n")

    def server_close(self):
        try:
            self.hub.save_if_dirty()
            self.write_status(running=False)
        finally:
            super().server_close()
