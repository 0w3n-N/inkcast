// Shared rendering, networking and export for the pad and the viewer.

export const COLORS = ['#111111', '#1f5fd6', '#d62f2f', '#1d9a4b', '#e07b00', '#7b3fd1', '#ffffff'];

// ---------------------------------------------------------------- auth & theme

// The key comes from the pairing link (?k=, the iPad) or from a one-time
// launch code (?c=, the desktop viewer) that is swapped for the key and then
// removed from the address bar. The viewer keeps it in sessionStorage so a
// reload still works; nothing is stored in cookies.
export async function getKey() {
  const params = new URLSearchParams(location.search);
  const code = params.get('c');
  if (code) {
    try {
      const r = await fetch('/api/redeem', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ c: code }),
      });
      if (r.ok) {
        const { k } = await r.json();
        try {
          sessionStorage.setItem('inkcast.k', k);
        } catch {}
        history.replaceState(null, '', location.pathname);
        return k;
      }
    } catch {}
    history.replaceState(null, '', location.pathname);
  }
  const k = params.get('k');
  if (k) return k;
  try {
    return sessionStorage.getItem('inkcast.k') || '';
  } catch {
    return '';
  }
}

export async function keyWorks(key) {
  if (!key) return false;
  try {
    return (await fetch(`/api/info?k=${encodeURIComponent(key)}`)).ok;
  } catch {
    return true; // server unreachable: let the reconnect loop handle it
  }
}

export function showLocked(message) {
  const el = document.createElement('div');
  el.id = 'locked';
  const card = document.createElement('div');
  card.className = 'card';
  const h = document.createElement('h1');
  h.textContent = 'Inkcast';
  const p = document.createElement('p');
  p.textContent = message;
  card.append(h, p);
  el.append(card);
  document.body.append(el);
}

// Live-updating link to the Omarchy theme stylesheet.
export function useTheme(key) {
  const link = document.createElement('link');
  link.rel = 'stylesheet';
  document.head.append(link);
  const set = (v) => (link.href = `/theme.css?k=${encodeURIComponent(key)}&v=${encodeURIComponent(v || '')}`);
  set(Date.now());
  return set;
}

// What to tell the user about desktop annotation (the "live" message).
export function liveNotice(msg) {
  if (msg.state === 'picking') return 'Choose the window in the picker on your computer…';
  if (msg.state === 'live') return 'The window is live. Draw on it.';
  if (msg.state === 'error') return msg.error || 'Window capture failed';
  if (msg.reason === 'cancelled') return 'Window sharing cancelled';
  if (msg.reason === 'closed') return 'The window closed. Its last frame stays on the page.';
  if (msg.reason === 'stopped') return 'Stopped. The last frame stays on the page.';
  if (msg.reason === 'error') return msg.error || 'Window capture stopped';
  return '';
}

// ---------------------------------------------------------------- connection

export function connect(role, handlers, k) {
  const url = `${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws?role=${role}&k=${encodeURIComponent(k)}`;
  let ws = null;
  let queue = [];
  let retry = 250;
  const api = {
    connected: false,
    key: k,
    send(msg) {
      const s = JSON.stringify(msg);
      if (ws && ws.readyState === 1) ws.send(s);
      else if (msg.t !== 'cursor') queue.push(s);
    },
  };
  function open() {
    ws = new WebSocket(url);
    ws.binaryType = 'blob';
    ws.onopen = () => {
      retry = 250;
      api.connected = true;
      handlers.status?.(true);
    };
    ws.onmessage = (e) => {
      if (typeof e.data !== 'string') return handlers.frame?.(e.data); // live window frame (JPEG)
      const msg = JSON.parse(e.data);
      handlers.message(msg);
      // Flush only after the fresh state arrived, so queued ops apply on top of it.
      if (msg.t === 'state' && queue.length) {
        for (const s of queue) ws.send(s);
        queue = [];
      }
    };
    ws.onclose = () => {
      api.connected = false;
      handlers.status?.(false);
      setTimeout(open, retry);
      retry = Math.min(retry * 2, 4000);
    };
  }
  open();
  return api;
}

// ---------------------------------------------------------------- strokes

// Width multiplier from pen pressure. Mouse/finger report 0.5.
const pressureWidth = (base, p) => base * (0.35 + 1.15 * Math.min(1, Math.max(0, p ?? 0.5)));

export function drawStroke(ctx, s, k) {
  const p = s.pts;
  if (!p || !p.length) return;
  ctx.lineCap = 'round';
  ctx.lineJoin = 'round';
  ctx.strokeStyle = s.color;
  ctx.fillStyle = s.color;
  if (s.tool === 'hl') {
    ctx.save();
    ctx.globalAlpha = 0.32;
    ctx.globalCompositeOperation = 'multiply';
    ctx.lineWidth = s.width * k;
    ctx.beginPath();
    ctx.moveTo(p[0][0] * k, p[0][1] * k);
    for (let i = 1; i < p.length - 1; i++) {
      const mx = (p[i][0] + p[i + 1][0]) / 2, my = (p[i][1] + p[i + 1][1]) / 2;
      ctx.quadraticCurveTo(p[i][0] * k, p[i][1] * k, mx * k, my * k);
    }
    const l = p[p.length - 1];
    ctx.lineTo(l[0] * k, l[1] * k);
    ctx.stroke();
    ctx.restore();
    return;
  }
  if (p.length === 1) {
    ctx.beginPath();
    ctx.arc(p[0][0] * k, p[0][1] * k, Math.max(0.1, (pressureWidth(s.width, p[0][2]) * k) / 2), 0, Math.PI * 2);
    ctx.fill();
    return;
  }
  // Quadratic curves through segment midpoints, one segment at a time so the
  // width can follow pressure.
  let x0 = p[0][0], y0 = p[0][1];
  for (let i = 1; i < p.length; i++) {
    const last = i === p.length - 1;
    const x1 = last ? p[i][0] : (p[i][0] + p[i + 1][0]) / 2;
    const y1 = last ? p[i][1] : (p[i][1] + p[i + 1][1]) / 2;
    ctx.lineWidth = pressureWidth(s.width, p[i][2]) * k;
    ctx.beginPath();
    ctx.moveTo(x0 * k, y0 * k);
    if (last) ctx.lineTo(x1 * k, y1 * k);
    else ctx.quadraticCurveTo(p[i][0] * k, p[i][1] * k, x1 * k, y1 * k);
    ctx.stroke();
    x0 = x1;
    y0 = y1;
  }
}

function segDist2(px, py, ax, ay, bx, by) {
  const dx = bx - ax, dy = by - ay;
  const l2 = dx * dx + dy * dy;
  let t = l2 ? ((px - ax) * dx + (py - ay) * dy) / l2 : 0;
  t = Math.max(0, Math.min(1, t));
  const x = ax + t * dx - px, y = ay + t * dy - py;
  return x * x + y * y;
}

export function strokeHit(s, x, y, r) {
  const p = s.pts;
  const rr = r + (s.width || 0) / 2;
  const r2 = rr * rr;
  if (p.length === 1) return (p[0][0] - x) ** 2 + (p[0][1] - y) ** 2 <= r2;
  for (let i = 1; i < p.length; i++) {
    if (segDist2(x, y, p[i - 1][0], p[i - 1][1], p[i][0], p[i][1]) <= r2) return true;
  }
  return false;
}

// ---------------------------------------------------------------- paper

export function drawPaper(ctx, page, k) {
  const w = page.w * k, h = page.h * k;
  ctx.fillStyle = page.bg?.live ? '#000000' : '#ffffff';
  ctx.fillRect(0, 0, w, h);
  const kind = page.paper || 'blank';
  if (kind === 'blank' || page.bg) return;
  const step = 32 * k;
  ctx.save();
  if (kind === 'dots') {
    ctx.fillStyle = '#b9c0cc';
    const r = Math.max(1, 1.6 * k);
    for (let y = step; y < h; y += step)
      for (let x = step; x < w; x += step) {
        ctx.beginPath();
        ctx.arc(x, y, r, 0, Math.PI * 2);
        ctx.fill();
      }
  } else {
    ctx.strokeStyle = kind === 'grid' ? '#dbe2ec' : '#c8d6ea';
    ctx.lineWidth = Math.max(1, k);
    ctx.beginPath();
    for (let y = (kind === 'lines' ? 2 : 1) * step; y < h; y += step) {
      ctx.moveTo(0, Math.round(y) + 0.5);
      ctx.lineTo(w, Math.round(y) + 0.5);
    }
    if (kind === 'grid')
      for (let x = step; x < w; x += step) {
        ctx.moveTo(Math.round(x) + 0.5, 0);
        ctx.lineTo(Math.round(x) + 0.5, h);
      }
    ctx.stroke();
  }
  ctx.restore();
}

// ---------------------------------------------------------------- background images

const imgCache = new Map();
export function pageImageURL(page, key) {
  const q = key ? `?k=${encodeURIComponent(key)}` : '';
  if (page.bg?.pdf) return `/api/pdf/${page.bg.pdf}/${page.bg.n}.png${q}`;
  if (page.bg?.img) return `/api/img/${page.bg.img}.jpg${q}`;
  return null; // blank paper, or a live window (drawn from the video layer)
}
export function loadImage(url, onload) {
  if (!url) return null;
  let e = imgCache.get(url);
  if (!e) {
    const img = new Image();
    e = { img, ready: false, waiters: [] };
    img.onload = () => {
      e.ready = true;
      e.waiters.splice(0).forEach((f) => f());
    };
    img.src = url;
    imgCache.set(url, e);
  }
  if (e.ready) return e.img;
  if (onload) e.waiters.push(onload);
  return null;
}

// ---------------------------------------------------------------- surface

// Stacked canvases: paper/PDF, live window video, committed ink, and
// in-progress strokes.
export class Surface {
  constructor(root, key) {
    this.root = root;
    this.key = key;
    this.bg = this.#canvas();
    this.media = this.#canvas();
    this.ink = this.#canvas();
    this.live = this.#canvas();
    this.page = null;
    this.active = new Map(); // stroke id -> stroke still being drawn
    this.view = { s: 1, x: 0, y: 0 }; // page unit -> css px
    this.dpr = 1;
    this.liveQueued = false;
    this.frame = null; // latest live-window frame (ImageBitmap)
    this.decoding = false;
    this.nextBlob = null;
    this.mediaQueued = false;
  }
  #canvas() {
    const c = document.createElement('canvas');
    this.root.appendChild(c);
    return c;
  }
  resize() {
    this.dpr = window.devicePixelRatio || 1;
    const r = this.root.getBoundingClientRect();
    this.cw = r.width;
    this.ch = r.height;
    for (const c of [this.bg, this.media, this.ink, this.live]) {
      c.width = Math.round(r.width * this.dpr);
      c.height = Math.round(r.height * this.dpr);
      c.style.width = r.width + 'px';
      c.style.height = r.height + 'px';
    }
  }
  fit(page = this.page, margin = 16) {
    if (!page) return;
    // Clamp: the stage can be 0x0 while hidden or mid-rotation.
    const s = Math.max(0.01, Math.min((this.cw - margin * 2) / page.w, (this.ch - margin * 2) / page.h));
    this.view = { s, x: (this.cw - page.w * s) / 2, y: (this.ch - page.h * s) / 2 };
  }
  toPage(cx, cy) {
    return [(cx - this.view.x) / this.view.s, (cy - this.view.y) / this.view.s];
  }
  #prep(c) {
    const ctx = c.getContext('2d');
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.clearRect(0, 0, c.width, c.height);
    ctx.setTransform(this.dpr, 0, 0, this.dpr, this.view.x * this.dpr, this.view.y * this.dpr);
    return ctx;
  }
  setPage(page) {
    this.page = page;
    this.redraw();
  }
  redraw() {
    this.drawBg();
    this.drawMedia();
    this.drawInk();
    this.drawLive();
  }
  // A new JPEG frame of the live window. If decoding falls behind, only the
  // newest frame is kept.
  pushFrame(blob) {
    if (this.decoding) {
      this.nextBlob = blob;
      return;
    }
    this.decoding = true;
    createImageBitmap(blob)
      .then((bmp) => {
        this.frame?.close?.();
        this.frame = bmp;
        this.queueMedia();
      })
      .catch(() => {})
      .finally(() => {
        this.decoding = false;
        const next = this.nextBlob;
        this.nextBlob = null;
        if (next) this.pushFrame(next);
      });
  }
  queueMedia() {
    if (this.mediaQueued) return;
    this.mediaQueued = true;
    requestAnimationFrame(() => {
      this.mediaQueued = false;
      this.drawMedia();
    });
  }
  drawMedia() {
    const ctx = this.#prep(this.media);
    const page = this.page;
    if (!page?.bg?.live || !this.frame) return;
    const k = this.view.s;
    ctx.imageSmoothingQuality = 'high';
    ctx.drawImage(this.frame, 0, 0, page.w * k, page.h * k);
  }
  drawBg() {
    const page = this.page;
    const ctx = this.#prep(this.bg);
    if (!page) return;
    const k = this.view.s;
    ctx.shadowColor = 'rgba(0,0,0,0.35)';
    ctx.shadowBlur = 18;
    ctx.fillStyle = page.bg?.live ? '#000' : '#fff';
    ctx.fillRect(0, 0, page.w * k, page.h * k);
    ctx.shadowColor = 'transparent';
    drawPaper(ctx, page, k);
    const url = pageImageURL(page, this.key);
    const img = loadImage(url, () => this.page === page && this.drawBg());
    if (img) {
      ctx.imageSmoothingQuality = 'high';
      ctx.drawImage(img, 0, 0, page.w * k, page.h * k);
    }
  }
  drawInk() {
    const ctx = this.#prep(this.ink);
    if (!this.page) return;
    for (const s of this.page.strokes) if (!this.active.has(s.id)) drawStroke(ctx, s, this.view.s);
  }
  commit(s) {
    this.active.delete(s.id);
    const ctx = this.ink.getContext('2d');
    ctx.setTransform(this.dpr, 0, 0, this.dpr, this.view.x * this.dpr, this.view.y * this.dpr);
    drawStroke(ctx, s, this.view.s);
    this.queueLive();
  }
  queueLive() {
    if (this.liveQueued) return;
    this.liveQueued = true;
    requestAnimationFrame(() => {
      this.liveQueued = false;
      this.drawLive();
    });
  }
  drawLive() {
    const ctx = this.#prep(this.live);
    for (const s of this.active.values()) drawStroke(ctx, s, this.view.s);
    this.overlay?.(ctx, this.view.s);
  }
}

// ---------------------------------------------------------------- document helpers

export function indexDoc(doc) {
  const pages = new Map(), strokes = new Map();
  for (const p of doc.pages) {
    pages.set(p.id, p);
    for (const s of p.strokes) strokes.set(s.id, s);
  }
  return { pages, strokes };
}

// Apply a relayed op to a local doc copy. Returns the affected page.
export function applyOp(doc, idx, msg) {
  const page = idx.pages.get(msg.page);
  switch (msg.t) {
    case 'begin': {
      if (!page || idx.strokes.has(msg.s.id)) return null;
      const s = { ...msg.s, pts: [...(msg.s.pts || [])] };
      page.strokes.push(s);
      idx.strokes.set(s.id, s);
      return page;
    }
    case 'pts': {
      const s = idx.strokes.get(msg.id);
      if (s) s.pts.push(...msg.pts);
      return null;
    }
    case 'remove': {
      if (!page) return null;
      const ids = new Set(msg.ids);
      page.strokes = page.strokes.filter((s) => !ids.has(s.id));
      ids.forEach((i) => idx.strokes.delete(i));
      return page;
    }
    case 'restore': {
      if (!page) return null;
      for (const s of msg.strokes) {
        if (idx.strokes.has(s.id)) continue;
        page.strokes.push(s);
        idx.strokes.set(s.id, s);
      }
      page.strokes.sort((a, b) => (a.z || 0) - (b.z || 0));
      return page;
    }
    case 'paper':
      if (page) page.paper = msg.paper;
      return page;
    case 'goto':
      doc.page = msg.index;
      return null;
  }
  return null;
}

// ---------------------------------------------------------------- PDF export

async function renderPageJPEG(page, key, px = 1800) {
  const k = px / page.w;
  const c = document.createElement('canvas');
  c.width = Math.round(page.w * k);
  c.height = Math.round(page.h * k);
  const ctx = c.getContext('2d');
  drawPaper(ctx, page, k);
  // A live window exports its current frame.
  const url = page.bg?.live
    ? `/api/live/frame.jpg?k=${encodeURIComponent(key)}&t=${Date.now()}`
    : pageImageURL(page, key);
  if (url) {
    const img = loadImage(url) || (await new Promise((res) => {
      loadImage(url, () => res(loadImage(url)));
      setTimeout(() => res(null), 8000); // e.g. no frame yet
    }));
    if (img) ctx.drawImage(img, 0, 0, c.width, c.height);
  }
  for (const s of page.strokes) drawStroke(ctx, s, k);
  const blob = await new Promise((res) => c.toBlob(res, 'image/jpeg', 0.9));
  return { bytes: new Uint8Array(await blob.arrayBuffer()), wpx: c.width, hpx: c.height };
}

// Minimal PDF writer: one full-page JPEG per page.
export async function exportPDF(doc, key, name = 'inkcast') {
  const enc = new TextEncoder();
  const chunks = [];
  const offsets = [];
  let pos = 0;
  const put = (x) => {
    const b = typeof x === 'string' ? enc.encode(x) : x;
    chunks.push(b);
    pos += b.length;
  };
  const n = doc.pages.length;
  // objects: 1 catalog, 2 pages, then per page i: page, content, image
  const pageObj = (i) => 3 + i * 3;
  put('%PDF-1.4\n%\xC7\xEC\x8F\xA2\n');
  offsets[1] = pos;
  put('1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n');
  offsets[2] = pos;
  put(`2 0 obj\n<< /Type /Pages /Count ${n} /Kids [${doc.pages.map((_, i) => `${pageObj(i)} 0 R`).join(' ')}] >>\nendobj\n`);
  for (let i = 0; i < n; i++) {
    const page = doc.pages[i];
    const img = await renderPageJPEG(page, key);
    const [w, h] = page.pt || [960, 960 * (page.h / page.w)];
    const po = pageObj(i);
    offsets[po] = pos;
    put(`${po} 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 ${w} ${h}] /Resources << /XObject << /Im0 ${po + 2} 0 R >> >> /Contents ${po + 1} 0 R >>\nendobj\n`);
    const content = `q ${w} 0 0 ${h} 0 0 cm /Im0 Do Q`;
    offsets[po + 1] = pos;
    put(`${po + 1} 0 obj\n<< /Length ${content.length} >>\nstream\n${content}\nendstream\nendobj\n`);
    offsets[po + 2] = pos;
    put(`${po + 2} 0 obj\n<< /Type /XObject /Subtype /Image /Width ${img.wpx} /Height ${img.hpx} /ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /DCTDecode /Length ${img.bytes.length} >>\nstream\n`);
    put(img.bytes);
    put('\nendstream\nendobj\n');
  }
  const count = 3 + n * 3;
  const xref = pos;
  let x = `xref\n0 ${count}\n0000000000 65535 f \n`;
  for (let i = 1; i < count; i++) x += `${String(offsets[i]).padStart(10, '0')} 00000 n \n`;
  put(x);
  put(`trailer\n<< /Size ${count} /Root 1 0 R >>\nstartxref\n${xref}\n%%EOF\n`);
  const blob = new Blob(chunks, { type: 'application/pdf' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = `${name.replace(/\.pdf$/i, '')}-inkcast-${new Date().toISOString().slice(0, 16).replace(/[:T]/g, '-')}.pdf`;
  document.body.appendChild(a);
  a.click();
  setTimeout(() => {
    URL.revokeObjectURL(a.href);
    a.remove();
  }, 5000);
}

export async function uploadPDF(file, key) {
  const r = await fetch(`/api/pdf?name=${encodeURIComponent(file.name)}&k=${encodeURIComponent(key)}`, {
    method: 'POST',
    body: file,
    headers: { 'Content-Type': 'application/pdf' },
  });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || `upload failed (${r.status})`);
  return j;
}
