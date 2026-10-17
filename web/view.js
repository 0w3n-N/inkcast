import { Surface, applyOp, connect, exportPDF, getKey, indexDoc, keyWorks, liveNotice, showLocked, uploadPDF, useTheme } from '/ink.js';

const $ = (s) => document.querySelector(s);
const stage = $('#stage');

// `inkcast annotate` on Windows/macOS opens this window with #annotate=1.
const askToAnnotate = new URLSearchParams(location.hash.slice(1)).get('annotate') === '1';
const key = await getKey();
const surface = new Surface(stage, key);
const setTheme = useTheme(key);
if (!key || !(await keyWorks(key))) {
  showLocked('This window has no access key. Open it from the Inkcast bar icon, or run: inkcast open');
  throw new Error('inkcast: no key');
}

let doc = null;
let idx = null;
let pads = 0;
let pairForced = null; // null = automatic, true/false = user toggled
let cursor = null; // {page, x, y}

// View: `fitted` follows the window size; otherwise the user zoomed/panned.
const MARGIN = 24;
const MIN_ZOOM = 0.5; // relative to fit
const MAX_ZOOM = 12;
let fitted = true;
let follow = true;
let liveState = 'idle';
let captureMode = 'native'; // 'browser' on Windows/macOS: this window captures the window itself
let source = null; // the window this share window is capturing (browser mode)

function setLive(state) {
  liveState = state;
  const b = $('#b-live');
  b.classList.toggle('on', state !== 'idle');
  b.textContent = state === 'idle' ? 'Window' : 'Stop window';
  b.title = state === 'idle' ? 'Annotate a desktop window (W)' : 'Stop the live window (W)';
}

const curPage = () => doc && doc.pages[doc.page];

const net = connect(
  'view',
  {
    status(ok) {
      $('#status').className = ok ? 'ok' : 'bad';
      $('#status').title = ok ? 'Connected' : 'Reconnecting…';
    },
    frame(blob) {
      surface.pushFrame(blob);
    },
    message(msg) {
      if (msg.t === 'theme') return setTheme(msg.v);
      if (msg.t === 'live') {
        if (msg.state === 'needs-desktop') return showCapturePrompt();
        if (msg.state === 'source-stop') return stopSource(false);
        setLive(msg.state === 'error' ? 'idle' : msg.state);
        const text = liveNotice(msg);
        if (text) toast(text, msg.state === 'picking' ? 0 : 3500);
        return;
      }
      if (msg.t === 'state') {
        captureMode = msg.capture || 'native';
        setLive(msg.live?.state || 'idle');
        if (source && msg.live?.state === 'live') source.confirmed = true;
        if (source?.confirmed && msg.live?.state !== 'live') stopSource(false);
        const samePage = doc && curPage()?.id === msg.doc.pages[msg.doc.page]?.id;
        doc = msg.doc;
        idx = indexDoc(doc);
        pads = msg.pads;
        showPage(!samePage);
        return syncPair();
      }
      if (msg.t === 'peers') {
        pads = msg.pads;
        return syncPair();
      }
      if (!doc) return;
      if (msg.t === 'cursor') {
        cursor = msg.hide ? null : { page: msg.page, x: msg.x, y: msg.y };
        if (cursor && cursor.page === curPage()?.id) keepInView(cursor.x, cursor.y);
        return surface.queueLive();
      }
      const before = doc.page;
      const page = applyOp(doc, idx, msg);
      if (msg.t === 'goto' && doc.page !== before) return showPage(true);
      const onPage = page && page === curPage();
      if (msg.t === 'begin' && onPage) {
        surface.active.set(msg.s.id, idx.strokes.get(msg.s.id));
        surface.queueLive();
      }
      if (msg.t === 'pts' && surface.active.has(msg.id)) {
        const p = msg.pts[msg.pts.length - 1];
        if (p) keepInView(p[0], p[1]);
        surface.queueLive();
      }
      if (msg.t === 'end') {
        const s = idx.strokes.get(msg.id);
        if (s && surface.active.has(s.id)) surface.commit(s);
      }
      if ((msg.t === 'remove' || msg.t === 'restore') && onPage) surface.drawInk();
      if (msg.t === 'paper' && onPage) surface.drawBg();
      if (msg.t !== 'pts') syncPair();
    },
  },
  key,
);

// Pen tip / hover cursor so viewers can follow along.
surface.overlay = (ctx, k) => {
  const page = curPage();
  let at = null;
  for (const s of surface.active.values()) {
    const p = s.pts[s.pts.length - 1];
    if (p) at = p;
  }
  if (!at && cursor && cursor.page === page?.id) at = [cursor.x, cursor.y];
  if (!at) return;
  ctx.save();
  ctx.beginPath();
  ctx.arc(at[0] * k, at[1] * k, Math.max(4, Math.min(14, 5 * k)), 0, Math.PI * 2);
  ctx.fillStyle = 'rgba(230, 60, 60, 0.35)';
  ctx.strokeStyle = 'rgba(230, 60, 60, 0.9)';
  ctx.lineWidth = 1.5;
  ctx.fill();
  ctx.stroke();
  ctx.restore();
};

function showPage(refit = true) {
  surface.active.clear();
  if (refit || fitted) fit();
  surface.setPage(curPage());
  $('#pageno').textContent = `${doc.page + 1} / ${doc.pages.length}`;
  $('#b-prev').disabled = doc.page === 0;
  $('#b-next').disabled = doc.page === doc.pages.length - 1;
}

function gotoPage(i) {
  if (!doc || i < 0 || i >= doc.pages.length || i === doc.page) return;
  doc.page = i;
  net.send({ t: 'goto', index: i });
  showPage(true);
}

// ---------------------------------------------------------------- zoom & pan

const fitScale = () => {
  const p = curPage();
  return p ? Math.max(0.01, Math.min((surface.cw - MARGIN * 2) / p.w, (surface.ch - MARGIN * 2) / p.h)) : 1;
};

function fit() {
  fitted = true;
  surface.fit(curPage(), MARGIN);
  syncZoomLabel();
}

let redrawQueued = false;
function queueRedraw() {
  if (redrawQueued) return;
  redrawQueued = true;
  requestAnimationFrame(() => {
    redrawQueued = false;
    surface.redraw();
  });
}

function setView(s, x, y) {
  const f = fitScale();
  const p = curPage();
  s = Math.min(f * MAX_ZOOM, Math.max(f * MIN_ZOOM, s));
  // Keep at least a sliver of the page on screen.
  const keep = 80;
  x = Math.min(surface.cw - keep, Math.max(keep - p.w * s, x));
  y = Math.min(surface.ch - keep, Math.max(keep - p.h * s, y));
  surface.view = { s, x, y };
  fitted = false;
  syncZoomLabel();
  queueRedraw();
}

function zoomAt(cx, cy, factor) {
  if (!doc) return;
  const v = surface.view;
  const s = Math.min(fitScale() * MAX_ZOOM, Math.max(fitScale() * MIN_ZOOM, v.s * factor));
  const r = s / v.s;
  setView(s, cx - (cx - v.x) * r, cy - (cy - v.y) * r);
}

function zoomCenter(factor) {
  zoomAt(surface.cw / 2, surface.ch / 2, factor);
}

function syncZoomLabel() {
  const pct = Math.round((surface.view.s / fitScale()) * 100);
  $('#b-zoom').textContent = fitted ? 'Fit' : `${pct}%`;
  $('#b-follow').hidden = fitted;
  document.body.classList.toggle('zoomed', !fitted);
}

// While zoomed in, slide the view to keep the pen in sight.
let followAnim = 0;
function keepInView(px, py) {
  if (fitted || !follow || dragging) return;
  const v = surface.view;
  const sx = v.x + px * v.s, sy = v.y + py * v.s;
  const mx = surface.cw * 0.15, my = surface.ch * 0.15;
  let dx = 0, dy = 0;
  if (sx < mx) dx = surface.cw / 2 - sx;
  else if (sx > surface.cw - mx) dx = surface.cw / 2 - sx;
  if (sy < my) dy = surface.ch / 2 - sy;
  else if (sy > surface.ch - my) dy = surface.ch / 2 - sy;
  if (!dx && !dy) return;
  const target = { x: v.x + dx, y: v.y + dy };
  cancelAnimationFrame(followAnim);
  const step = () => {
    const c = surface.view;
    const nx = c.x + (target.x - c.x) * 0.25, ny = c.y + (target.y - c.y) * 0.25;
    setView(c.s, nx, ny);
    if (Math.abs(target.x - nx) + Math.abs(target.y - ny) > 1) followAnim = requestAnimationFrame(step);
  };
  followAnim = requestAnimationFrame(step);
}

// Mouse wheel zooms at the pointer; a touchpad's two-finger scroll pans and
// its pinch (reported as ctrl+wheel) zooms.
stage.addEventListener(
  'wheel',
  (e) => {
    e.preventDefault();
    if (!doc) return;
    const r = stage.getBoundingClientRect();
    const cx = e.clientX - r.left, cy = e.clientY - r.top;
    const lines = e.deltaMode === 1;
    const mouseWheel = lines || (e.deltaX === 0 && Math.abs(e.deltaY) >= 40 && Number.isInteger(e.deltaY));
    if (e.ctrlKey) zoomAt(cx, cy, Math.exp(-e.deltaY * 0.01));
    else if (mouseWheel) {
      // One notch is ~100px (or 3 lines); bigger deltas mean a faster spin.
      const notches = Math.min(4, Math.max(1, Math.abs(e.deltaY) / (lines ? 3 : 100)));
      zoomAt(cx, cy, Math.pow(e.deltaY < 0 ? 1.18 : 1 / 1.18, notches));
    }
    else setView(surface.view.s, surface.view.x - e.deltaX, surface.view.y - e.deltaY);
    wake();
  },
  { passive: false },
);

// Grab and drag to move around.
let dragging = null;
stage.addEventListener('pointerdown', (e) => {
  if (e.button !== 0 && e.button !== 1) return;
  if (!doc) return;
  cancelAnimationFrame(followAnim);
  dragging = { id: e.pointerId, x: e.clientX, y: e.clientY, vx: surface.view.x, vy: surface.view.y };
  try {
    stage.setPointerCapture(e.pointerId);
  } catch {}
  document.body.classList.add('dragging');
});
stage.addEventListener('pointermove', (e) => {
  if (!dragging || e.pointerId !== dragging.id) return;
  const dx = e.clientX - dragging.x, dy = e.clientY - dragging.y;
  if (fitted && Math.abs(dx) + Math.abs(dy) < 3) return;
  setView(surface.view.s, dragging.vx + dx, dragging.vy + dy);
});
const endDrag = (e) => {
  if (!dragging || e.pointerId !== dragging.id) return;
  dragging = null;
  document.body.classList.remove('dragging');
};
stage.addEventListener('pointerup', endDrag);
stage.addEventListener('pointercancel', endDrag);
stage.addEventListener('dblclick', (e) => {
  if (!doc) return;
  if (fitted) {
    const r = stage.getBoundingClientRect();
    zoomAt(e.clientX - r.left, e.clientY - r.top, 2);
  } else {
    fit();
    surface.redraw();
  }
});

// ---------------------------------------------------------------- pairing card

let infoLoaded = false;
async function loadInfo() {
  if (infoLoaded) return;
  try {
    const r = await fetch(`/api/info?k=${encodeURIComponent(key)}`);
    if (!r.ok) return;
    const info = await r.json();
    const url = info.urls[0];
    $('#url').textContent = url || 'No Wi-Fi address found. Connect to a network, then restart Inkcast.';
    const qr = $('#qr');
    qr.onerror = () => (qr.hidden = true);
    if (url) qr.src = `/api/qr.svg?k=${encodeURIComponent(key)}&t=${Date.now()}`;
    else qr.hidden = true;
    infoLoaded = true;
  } catch {}
}

function syncPair() {
  const empty = doc && doc.pages.every((p) => !p.strokes.length && !p.bg) && !doc.pdf;
  const show = pairForced ?? (pads === 0 && empty);
  $('#pair').hidden = !show;
  if (show) loadInfo();
}

// ---------------------------------------------------------------- controls

let toastTimer = 0;
function toast(text, ms = 2500) {
  const t = $('#toast');
  t.textContent = text;
  t.hidden = false;
  clearTimeout(toastTimer);
  if (ms) toastTimer = setTimeout(() => (t.hidden = true), ms);
}

async function openPDF(f) {
  if (!f) return;
  toast(`Opening ${f.name}…`, 0);
  try {
    const r = await uploadPDF(f, key);
    toast(`Opened ${f.name} (${r.pages} pages)`);
    pairForced = false;
  } catch (err) {
    toast(err.message, 5000);
  }
}

async function save() {
  if (!doc) return;
  toast('Building PDF…', 0);
  try {
    await exportPDF(doc, key, doc.pdf?.name || 'notebook');
    toast('Saved to your Downloads folder');
  } catch (err) {
    toast('Export failed: ' + err.message, 5000);
  }
}

function togglePair() {
  pairForced = $('#pair').hidden;
  syncPair();
}

function toggleLive() {
  if (source) return stopSource(true);
  if (liveState !== 'idle') return net.send({ t: 'live', action: 'stop' });
  if (captureMode === 'browser') return startSource(); // needs this click for the picker
  net.send({ t: 'live', action: 'start' });
}

// ---------------------------------------------------------------- browser window capture
// Windows and macOS have no screen-share portal Inkcast can use, so this window
// captures the chosen window with the browser's own picker and streams JPEG
// frames to the server, which relays them to the iPad.

const MAX_W = 1600;
const FPS = 15;

function showCapturePrompt() {
  if (source || captureMode !== 'browser') return;
  $('#capture-prompt').hidden = false;
}

async function startSource() {
  $('#capture-prompt').hidden = true;
  if (!navigator.mediaDevices?.getDisplayMedia) return toast('This browser cannot capture windows. Use Chrome or Edge.', 5000);
  let stream;
  try {
    stream = await navigator.mediaDevices.getDisplayMedia({
      video: { displaySurface: 'window', frameRate: { ideal: FPS, max: FPS } },
      audio: false,
      selfBrowserSurface: 'exclude',
      surfaceSwitching: 'exclude',
    });
  } catch (e) {
    return toast(e.name === 'NotAllowedError' ? 'Window sharing cancelled' : 'Could not capture the window: ' + e.message, 4000);
  }
  const track = stream.getVideoTracks()[0];
  const st = track.getSettings();
  source = { stream, track, w: st.width || 1280, h: st.height || 720, last: 0, busy: false, stopped: false, confirmed: false };
  track.addEventListener('ended', () => stopSource(true)); // "Stop sharing" in the browser
  net.send({ t: 'live', action: 'source', w: source.w, h: source.h });
  pump(source);
}

async function pump(src) {
  const canvas = document.createElement('canvas');
  const ctx = canvas.getContext('2d');
  const send = async (image, w, h) => {
    if (!w || !h) return;
    if (w !== src.w || h !== src.h) {
      src.w = w;
      src.h = h;
      net.send({ t: 'live', action: 'resize', w, h });
    }
    const k = Math.min(1, MAX_W / w);
    const cw = Math.max(2, Math.round(w * k) & ~1), ch = Math.max(2, Math.round(h * k) & ~1);
    if (canvas.width !== cw || canvas.height !== ch) {
      canvas.width = cw;
      canvas.height = ch;
    }
    ctx.drawImage(image, 0, 0, cw, ch);
    const blob = await new Promise((res) => canvas.toBlob(res, 'image/jpeg', 0.72));
    // Skip frames rather than queue them if the connection is backed up.
    if (blob && !src.stopped && net.bufferedAmount() < 2_000_000) net.sendBinary(blob);
  };
  if ('MediaStreamTrackProcessor' in window) {
    // Frames arrive as the window changes, even while this window is in the background.
    const reader = new MediaStreamTrackProcessor({ track: src.track }).readable.getReader();
    src.reader = reader;
    while (!src.stopped) {
      let res;
      try {
        res = await reader.read();
      } catch {
        break;
      }
      if (res.done) break;
      const frame = res.value;
      const now = performance.now();
      if (!src.busy && now - src.last >= 1000 / FPS - 4) {
        src.last = now;
        src.busy = true;
        try {
          await send(frame, frame.displayWidth, frame.displayHeight);
        } catch {}
        src.busy = false;
      }
      frame.close();
    }
  } else {
    const video = document.createElement('video');
    video.muted = true;
    video.playsInline = true;
    video.srcObject = src.stream;
    await video.play().catch(() => {});
    src.timer = setInterval(async () => {
      if (src.busy || src.stopped) return;
      src.busy = true;
      try {
        await send(video, video.videoWidth, video.videoHeight);
      } catch {}
      src.busy = false;
    }, 1000 / FPS);
  }
}

function stopSource(notify) {
  if (!source) return;
  const src = source;
  source = null;
  src.stopped = true;
  src.reader?.cancel().catch(() => {});
  clearInterval(src.timer);
  src.stream.getTracks().forEach((t) => t.stop());
  if (notify) net.send({ t: 'live', action: 'stop' });
}

function toggleFollow() {
  follow = !follow;
  $('#b-follow').classList.toggle('on', follow);
  toast(follow ? 'Following the pen' : 'Not following the pen', 1200);
}

function fullscreen() {
  if (document.fullscreenElement) document.exitFullscreen();
  else document.documentElement.requestFullscreen().catch(() => {});
}

function resetZoom() {
  fit();
  surface.redraw();
}

$('#b-prev').onclick = () => gotoPage((doc?.page ?? 0) - 1);
$('#b-next').onclick = () => gotoPage((doc?.page ?? 0) + 1);
$('#b-zin').onclick = () => zoomCenter(1.25);
$('#b-zout').onclick = () => zoomCenter(1 / 1.25);
$('#b-zoom').onclick = resetZoom;
$('#b-follow').onclick = toggleFollow;
$('#b-qr').onclick = togglePair;
$('#b-live').onclick = toggleLive;
$('#b-choose').onclick = startSource;
$('#b-choose-cancel').onclick = () => ($('#capture-prompt').hidden = true);
$('#b-pdf').onclick = () => $('#file').click();
$('#b-export').onclick = save;
$('#b-full').onclick = fullscreen;
$('#file').onchange = (e) => {
  openPDF(e.target.files[0]);
  e.target.value = '';
};
$('#pair').onclick = (e) => {
  if (e.target === $('#pair')) togglePair();
};

document.addEventListener('keydown', (e) => {
  if (e.ctrlKey || e.metaKey || e.altKey) return;
  const k = e.key.toLowerCase();
  if (k === 'q') togglePair();
  else if (k === 'f') fullscreen();
  else if (k === 'o') $('#file').click();
  else if (k === 's') save();
  else if (k === 'l') toggleFollow();
  else if (k === 'w') toggleLive();
  else if (k === '0') resetZoom();
  else if (k === '+' || k === '=') zoomCenter(1.25);
  else if (k === '-' || k === '_') zoomCenter(1 / 1.25);
  else if (k === 'arrowright' || k === 'pagedown' || k === ' ') gotoPage((doc?.page ?? 0) + 1);
  else if (k === 'arrowleft' || k === 'pageup') gotoPage((doc?.page ?? 0) - 1);
  else if (k === 'escape' && !$('#capture-prompt').hidden) $('#capture-prompt').hidden = true;
  else if (k === 'escape' && !$('#pair').hidden) togglePair();
  else return;
  e.preventDefault();
});

// Hide the controls and cursor when the mouse is idle so screen shares stay clean.
let idleTimer = 0;
function wake() {
  document.body.classList.add('awake');
  clearTimeout(idleTimer);
  idleTimer = setTimeout(() => document.body.classList.remove('awake'), 2200);
}
document.addEventListener('mousemove', wake);

// Drag and drop a PDF onto the window.
let dragDepth = 0;
const hasFiles = (e) => [...(e.dataTransfer?.types || [])].includes('Files');
document.addEventListener('dragenter', (e) => {
  if (!hasFiles(e)) return;
  e.preventDefault();
  dragDepth++;
  $('#drop').hidden = false;
});
document.addEventListener('dragleave', (e) => {
  if (!hasFiles(e)) return;
  if (--dragDepth <= 0) $('#drop').hidden = true;
});
document.addEventListener('dragover', (e) => e.preventDefault());
document.addEventListener('drop', (e) => {
  e.preventDefault();
  dragDepth = 0;
  $('#drop').hidden = true;
  const f = [...(e.dataTransfer?.files || [])].find((f) => f.type === 'application/pdf' || /\.pdf$/i.test(f.name));
  if (f) openPDF(f);
  else toast('That is not a PDF');
});

function layout() {
  const before = { w: surface.cw, h: surface.ch };
  surface.resize();
  if (!doc) return;
  if (fitted) return showPage(true);
  // Keep the same point centered when the window changes size.
  const v = surface.view;
  setView(v.s, v.x + (surface.cw - before.w) / 2, v.y + (surface.ch - before.h) / 2);
}
window.addEventListener('resize', layout);
layout();
if (askToAnnotate) {
  captureMode = 'browser';
  showCapturePrompt();
}
