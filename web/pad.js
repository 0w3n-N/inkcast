import { COLORS, Surface, applyOp, connect, exportPDF, indexDoc, strokeHit, uploadPDF } from '/ink.js';

const $ = (s) => document.querySelector(s);
const stage = $('#stage');
const key = new URLSearchParams(location.search).get('k') || '';
const surface = new Surface(stage, key);

const HL_WIDTH = { 2: 14, 3.5: 22, 6.5: 34 };
const ERASER_R = 10;
// Apple Pencil's barrel double-tap is not exposed to web pages, so a quick
// double-tap of the tip on the same spot toggles pen <-> eraser instead.
const TAP_MAX_MS = 200; // a tap is shorter than this...
const TAP_MAX_PX = 6; // ...and moves less than this
const DOUBLE_TAP_GAP_MS = 300; // between lifting the first tap and touching down again
const DOUBLE_TAP_PX = 16; // the two taps land this close together

let doc = null;
let idx = null;
let tool = 'pen';
let color = COLORS[0];
let width = 3.5;
let fingerDraw = localStorage.getItem('inkcast.finger') === '1';
let doubleTapToggle = localStorage.getItem('inkcast.doubletap') !== '0';
let lastTool = 'pen';
let lastTap = null; // {up, x, y, undoLen, redo}
let undo = [];
let redo = [];
let zoomed = false;

const curPage = () => doc && doc.pages[doc.page];
const newId = () => Math.random().toString(36).slice(2, 10) + Date.now().toString(36).slice(-4);
let zCounter = 0;
const nextZ = () => Date.now() * 1000 + (zCounter++ % 1000);

// ---------------------------------------------------------------- network

const net = connect('pad', {
  status(ok) {
    $('#dot').className = ok ? 'ok' : 'bad';
  },
  message(msg) {
    if (msg.t === 'state') return loadState(msg.doc);
    if (msg.t === 'peers') return;
    if (!doc) return;
    const before = doc.page;
    const page = applyOp(doc, idx, msg);
    if (msg.t === 'goto' && doc.page !== before) return showPage(true);
    if (msg.t === 'begin' && page === curPage()) surface.active.set(msg.s.id, idx.strokes.get(msg.s.id));
    if (msg.t === 'pts') surface.queueLive();
    if (msg.t === 'end') {
      const s = idx.strokes.get(msg.id);
      if (s && surface.active.has(s.id)) surface.commit(s);
    }
    if ((msg.t === 'remove' || msg.t === 'restore') && page === curPage()) surface.drawInk();
    if (msg.t === 'paper' && page === curPage()) surface.drawBg();
  },
});

function loadState(newDoc) {
  const sameDoc = doc && doc.id === newDoc.id;
  const keepView = sameDoc && curPage() && newDoc.pages[newDoc.page]?.id === curPage().id;
  doc = newDoc;
  idx = indexDoc(doc);
  if (!sameDoc) {
    undo = [];
    redo = [];
  }
  // Keep the stroke being drawn right now (the server copy may lag behind).
  if (drawing?.stroke) {
    const page = idx.pages.get(drawing.page);
    const i = page ? page.strokes.findIndex((s) => s.id === drawing.stroke.id) : -1;
    if (i >= 0) {
      page.strokes[i] = drawing.stroke;
      idx.strokes.set(drawing.stroke.id, drawing.stroke);
    } else drawing = null;
  }
  showPage(!keepView);
}

function showPage(refit) {
  surface.active.clear();
  if (drawing?.stroke && drawing.page === curPage().id) surface.active.set(drawing.stroke.id, drawing.stroke);
  if (refit) {
    surface.fit(curPage(), 12);
    setZoomed(false);
  }
  surface.setPage(curPage());
  $('#pageno').textContent = `${doc.page + 1} / ${doc.pages.length}`;
  $('#prev').disabled = doc.page === 0;
  $('#next').disabled = doc.page === doc.pages.length - 1;
  for (const b of document.querySelectorAll('[data-paper]')) b.classList.toggle('on', b.dataset.paper === (curPage().paper || 'blank'));
  // Warm the cache for the next PDF page.
  const nxt = doc.pages[doc.page + 1];
  if (nxt?.bg) new Image().src = `/api/pdf/${nxt.bg.pdf}/${nxt.bg.n}.png?k=${encodeURIComponent(key)}`;
}

function gotoPage(i) {
  if (!doc || i < 0 || i >= doc.pages.length || i === doc.page) return;
  doc.page = i;
  net.send({ t: 'goto', index: i });
  showPage(true);
}

// ---------------------------------------------------------------- drawing

function capture(e) {
  try {
    stage.setPointerCapture(e.pointerId);
  } catch {}
}

let drawing = null; // {pointerId, page, stroke | null (eraser), removed}
let pending = [];
let flushQueued = false;
let eraserAt = null;
let hoverQueued = null;

surface.overlay = (ctx, k) => {
  if (eraserAt) {
    ctx.strokeStyle = 'rgba(80,80,80,0.8)';
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    ctx.arc(eraserAt[0] * k, eraserAt[1] * k, eraserAt[2] * k, 0, Math.PI * 2);
    ctx.stroke();
  }
};

function flush() {
  flushQueued = false;
  if (drawing?.stroke && pending.length) net.send({ t: 'pts', id: drawing.stroke.id, pts: pending });
  pending = [];
}

function point(e) {
  const r = stage.getBoundingClientRect();
  const [x, y] = surface.toPage(e.clientX - r.left, e.clientY - r.top);
  const p = e.pointerType === 'pen' ? Math.max(0.05, e.pressure || 0.5) : 0.5;
  return [Math.round(x * 100) / 100, Math.round(y * 100) / 100, Math.round(p * 1000) / 1000];
}

function startDraw(e) {
  const page = curPage();
  const pt = point(e);
  capture(e);
  const base = { pointerId: e.pointerId, pointerType: e.pointerType, page: page.id, t0: performance.now(), x0: e.clientX, y0: e.clientY, moved: 0 };
  if (tool === 'eraser') {
    drawing = { ...base, stroke: null, removed: [] };
    eraseAt(pt);
    return;
  }
  const s = {
    id: newId(),
    z: nextZ(),
    tool,
    color: tool === 'hl' && color === '#111111' ? '#ffd400' : color,
    width: tool === 'hl' ? HL_WIDTH[width] : width,
    pts: [pt],
  };
  page.strokes.push(s);
  idx.strokes.set(s.id, s);
  drawing = { ...base, stroke: s };
  surface.active.set(s.id, s);
  net.send({ t: 'begin', page: page.id, s });
  surface.queueLive();
}

function moveDraw(e) {
  const events = e.getCoalescedEvents ? e.getCoalescedEvents() : [];
  const list = events.length ? events : [e];
  for (const ev of list) drawing.moved = Math.max(drawing.moved, Math.hypot(ev.clientX - drawing.x0, ev.clientY - drawing.y0));
  if (!drawing.stroke) {
    for (const ev of list) eraseAt(point(ev));
    return;
  }
  const pts = drawing.stroke.pts;
  for (const ev of list) {
    const pt = point(ev);
    const last = pts[pts.length - 1];
    if (Math.abs(pt[0] - last[0]) + Math.abs(pt[1] - last[1]) < 0.35) continue;
    pts.push(pt);
    pending.push(pt);
  }
  if (!flushQueued && pending.length) {
    flushQueued = true;
    requestAnimationFrame(flush);
  }
  surface.queueLive();
}

function endDraw(cancel = false) {
  flush();
  const d = drawing;
  drawing = null;
  const undoLen = undo.length;
  const redoBefore = redo.slice();
  if (d.stroke) {
    if (cancel) {
      removeStrokes(d.page, [d.stroke.id], true);
      return;
    }
    net.send({ t: 'end', id: d.stroke.id });
    surface.commit(d.stroke);
    pushUndo({ t: 'add', page: d.page, strokes: [d.stroke] });
  } else {
    eraserAt = null;
    surface.queueLive();
    if (d.removed.length) pushUndo({ t: 'erase', page: d.page, strokes: d.removed });
  }
  checkDoubleTap(d, undoLen, redoBefore);
}

function checkDoubleTap(d, undoLen, redoBefore) {
  const now = performance.now();
  const isTap = doubleTapToggle && d.pointerType === 'pen' && now - d.t0 < TAP_MAX_MS && d.moved < TAP_MAX_PX;
  if (!isTap) {
    lastTap = null;
    return;
  }
  const prev = lastTap;
  if (prev && d.t0 - prev.up < DOUBLE_TAP_GAP_MS && Math.hypot(d.x0 - prev.x, d.y0 - prev.y) < DOUBLE_TAP_PX) {
    // Take back whatever the two taps did (dots drawn or strokes erased).
    while (undo.length > prev.undoLen) applyHistory(undo.pop(), true);
    redo = prev.redo;
    syncUndoButtons();
    lastTap = null;
    toggleEraser();
    return;
  }
  lastTap = { up: now, x: d.x0, y: d.y0, undoLen, redo: redoBefore };
}

function toggleEraser() {
  setTool(tool === 'eraser' ? lastTool : 'eraser');
  toast(tool === 'eraser' ? 'Eraser' : tool === 'hl' ? 'Highlighter' : 'Pen', 700);
}

function eraseAt(pt) {
  const page = curPage();
  const r = ERASER_R / Math.max(0.5, surface.view.s / fitScale());
  // Sample the path since the last position so fast swipes don't skip strokes.
  const prev = eraserAt || pt;
  const steps = Math.max(1, Math.ceil(Math.hypot(pt[0] - prev[0], pt[1] - prev[1]) / (r / 2)));
  eraserAt = [pt[0], pt[1], r];
  const hits = page.strokes.filter((s) => {
    for (let i = 1; i <= steps; i++) {
      const t = i / steps;
      if (strokeHit(s, prev[0] + (pt[0] - prev[0]) * t, prev[1] + (pt[1] - prev[1]) * t, r)) return true;
    }
    return false;
  });
  if (hits.length) {
    drawing.removed.push(...hits);
    removeStrokes(page.id, hits.map((s) => s.id), true);
  }
  surface.queueLive();
}

function removeStrokes(pageId, ids, send) {
  applyOp(doc, idx, { t: 'remove', page: pageId, ids });
  ids.forEach((i) => surface.active.delete(i));
  if (send) net.send({ t: 'remove', page: pageId, ids });
  if (curPage().id === pageId) {
    surface.drawInk();
    surface.queueLive();
  }
}

function restoreStrokes(pageId, strokes) {
  applyOp(doc, idx, { t: 'restore', page: pageId, strokes });
  net.send({ t: 'restore', page: pageId, strokes });
  if (curPage().id === pageId) surface.drawInk();
}

function pushUndo(op) {
  undo.push(op);
  if (undo.length > 300) undo.shift();
  redo = [];
  syncUndoButtons();
}

function applyHistory(op, inverse) {
  const page = doc.pages.findIndex((p) => p.id === op.page);
  if (page < 0) return false;
  if (page !== doc.page) gotoPage(page);
  const add = (op.t === 'add') !== inverse;
  if (add) restoreStrokes(op.page, op.strokes);
  else removeStrokes(op.page, op.strokes.map((s) => s.id), true);
  return true;
}

function doUndo() {
  while (undo.length) {
    const op = undo.pop();
    if (applyHistory(op, true)) {
      redo.push(op);
      break;
    }
  }
  syncUndoButtons();
}

function doRedo() {
  while (redo.length) {
    const op = redo.pop();
    if (applyHistory(op, false)) {
      undo.push(op);
      break;
    }
  }
  syncUndoButtons();
}

function syncUndoButtons() {
  $('#undo').disabled = !undo.length;
  $('#redo').disabled = !redo.length;
}

// ---------------------------------------------------------------- gestures

const touches = new Map();
let gesture = null;
let viewQueued = false;

function fitScale() {
  const p = curPage();
  return Math.max(0.01, Math.min((surface.cw - 24) / p.w, (surface.ch - 24) / p.h));
}

function setZoomed(z) {
  zoomed = z;
  $('#fitbtn').hidden = !z;
}

function beginGesture() {
  const pts = [...touches.values()];
  const c = centroid(pts);
  gesture = { view: { ...surface.view }, c, d: pts.length > 1 ? dist(pts[0], pts[1]) : 0, n: pts.length };
}
const centroid = (pts) => pts.reduce((a, p) => [a[0] + p.x / pts.length, a[1] + p.y / pts.length], [0, 0]);
const dist = (a, b) => Math.hypot(a.x - b.x, a.y - b.y) || 1;

function moveGesture() {
  const pts = [...touches.values()];
  if (pts.length !== gesture.n) beginGesture();
  const c = centroid(pts);
  const v0 = gesture.view;
  let s = v0.s;
  if (pts.length > 1) {
    const f = fitScale();
    s = Math.min(f * 8, Math.max(f * 0.6, v0.s * (dist(pts[0], pts[1]) / gesture.d)));
  }
  const px = (gesture.c[0] - v0.x) / v0.s, py = (gesture.c[1] - v0.y) / v0.s;
  surface.view = { s, x: c[0] - px * s, y: c[1] - py * s };
  setZoomed(true);
  if (!viewQueued) {
    viewQueued = true;
    requestAnimationFrame(() => {
      viewQueued = false;
      surface.redraw();
    });
  }
}

function local(e) {
  const r = stage.getBoundingClientRect();
  return { x: e.clientX - r.left, y: e.clientY - r.top };
}

stage.addEventListener('pointerdown', (e) => {
  if (!doc) return;
  closeMenu();
  const penLike = e.pointerType === 'pen' || (e.pointerType === 'mouse' && e.button === 0);
  if (penLike) {
    if (drawing) return;
    // A pen down cancels any finger gesture in progress.
    touches.clear();
    gesture = null;
    return startDraw(e);
  }
  if (e.pointerType !== 'touch') return;
  if (drawing && drawing.pointerType === 'pen') return; // palm while writing
  touches.set(e.pointerId, local(e));
  if (fingerDraw && touches.size === 1 && !drawing) {
    return startDraw(e);
  }
  if (drawing && drawing.pointerType === 'touch') endDraw(true); // second finger: it was a gesture
  capture(e);
  beginGesture();
});

stage.addEventListener('pointermove', (e) => {
  if (drawing && e.pointerId === drawing.pointerId) {
    if (e.pointerType === 'touch') touches.set(e.pointerId, local(e));
    return moveDraw(e);
  }
  if (touches.has(e.pointerId)) {
    touches.set(e.pointerId, local(e));
    if (gesture) moveGesture();
    return;
  }
  // Pencil hover (iPad Pro) or mouse hover: show a cursor on the viewer.
  if ((e.pointerType === 'pen' || e.pointerType === 'mouse') && e.buttons === 0 && doc) {
    const [x, y] = point(e);
    if (!hoverQueued) requestAnimationFrame(() => {
      net.send({ t: 'cursor', page: curPage().id, x: hoverQueued[0], y: hoverQueued[1] });
      hoverQueued = null;
    });
    hoverQueued = [x, y];
  }
});

function pointerEnd(e) {
  if (drawing && e.pointerId === drawing.pointerId) {
    touches.delete(e.pointerId);
    endDraw(e.type === 'pointercancel' && drawing.pointerType === 'touch');
    return;
  }
  if (touches.delete(e.pointerId)) {
    if (touches.size) beginGesture();
    else gesture = null;
  }
}
stage.addEventListener('pointerup', pointerEnd);
stage.addEventListener('pointercancel', pointerEnd);
stage.addEventListener('pointerleave', (e) => {
  if (e.pointerType !== 'touch' && !drawing) net.send({ t: 'cursor', hide: true });
});

// Keep iOS from scrolling, zooming, or popping the magnifier.
for (const ev of ['touchstart', 'touchmove', 'gesturestart', 'gesturechange'])
  stage.addEventListener(ev, (e) => e.preventDefault(), { passive: false });
document.addEventListener('dblclick', (e) => e.preventDefault());

// ---------------------------------------------------------------- toolbar

const colorsEl = $('#colors');
for (const c of COLORS) {
  const b = document.createElement('button');
  b.className = 'swatch' + (c === color ? ' on' : '');
  b.style.setProperty('--c', c);
  b.title = c;
  b.onclick = () => {
    color = c;
    colorsEl.querySelectorAll('button').forEach((x) => x.classList.toggle('on', x === b));
    if (tool === 'eraser') setTool('pen');
  };
  colorsEl.appendChild(b);
}

function setTool(t) {
  if (t === 'eraser' && tool !== 'eraser') lastTool = tool;
  tool = t;
  document.querySelectorAll('[data-tool]').forEach((b) => b.classList.toggle('on', b.dataset.tool === t));
}
document.querySelectorAll('[data-tool]').forEach((b) => (b.onclick = () => setTool(b.dataset.tool)));
document.querySelectorAll('[data-w]').forEach(
  (b) =>
    (b.onclick = () => {
      width = +b.dataset.w;
      document.querySelectorAll('[data-w]').forEach((x) => x.classList.toggle('on', x === b));
      if (tool === 'eraser') setTool('pen');
    }),
);

$('#undo').onclick = doUndo;
$('#redo').onclick = doRedo;
$('#prev').onclick = () => gotoPage((doc?.page ?? 0) - 1);
$('#next').onclick = () => gotoPage((doc?.page ?? 0) + 1);
$('#addpage').onclick = () => net.send({ t: 'addpage' });
$('#fitbtn').onclick = () => showPage(true);

const menu = $('#menu');
function closeMenu() {
  menu.hidden = true;
}
$('#menubtn').onclick = (e) => {
  e.stopPropagation();
  menu.hidden = !menu.hidden;
};
$('#openpdf').onclick = () => {
  closeMenu();
  $('#file').click();
};
$('#file').onchange = async (e) => {
  const f = e.target.files[0];
  e.target.value = '';
  if (!f) return;
  toast(`Uploading ${f.name}…`, 0);
  try {
    const r = await uploadPDF(f, key);
    toast(`Opened ${f.name} (${r.pages} pages)`);
  } catch (err) {
    toast(err.message, 5000);
  }
};
$('#newdoc').onclick = () => {
  closeMenu();
  if (confirm('Start a new blank notebook? The current notebook will be replaced (save it as PDF first if you want to keep it).'))
    net.send({ t: 'newdoc' });
};
$('#clear').onclick = () => {
  closeMenu();
  const page = curPage();
  if (!page.strokes.length) return;
  const strokes = [...page.strokes];
  removeStrokes(page.id, strokes.map((s) => s.id), true);
  pushUndo({ t: 'erase', page: page.id, strokes });
};
$('#delpage').onclick = () => {
  closeMenu();
  if (doc.pages.length > 1 && confirm('Delete this page?')) net.send({ t: 'delpage', page: curPage().id });
};
document.querySelectorAll('[data-paper]').forEach(
  (b) =>
    (b.onclick = () => {
      const page = curPage();
      page.paper = b.dataset.paper;
      net.send({ t: 'paper', page: page.id, paper: page.paper });
      showPage(false);
    }),
);
const dt = $('#doubletap');
dt.checked = doubleTapToggle;
dt.onchange = () => {
  doubleTapToggle = dt.checked;
  localStorage.setItem('inkcast.doubletap', doubleTapToggle ? '1' : '0');
};
const fd = $('#fingerdraw');
fd.checked = fingerDraw;
fd.onchange = () => {
  fingerDraw = fd.checked;
  localStorage.setItem('inkcast.finger', fingerDraw ? '1' : '0');
};
$('#export').onclick = async () => {
  closeMenu();
  toast('Building PDF…', 0);
  try {
    await exportPDF(doc, key, doc.pdf?.name || 'notebook');
    toast('PDF saved');
  } catch (err) {
    toast('Export failed: ' + err.message, 5000);
  }
};
document.addEventListener('pointerdown', (e) => {
  if (!menu.hidden && !menu.contains(e.target) && e.target !== $('#menubtn')) closeMenu();
});

let toastTimer = 0;
function toast(text, ms = 2500) {
  const t = $('#toast');
  t.textContent = text;
  t.hidden = false;
  clearTimeout(toastTimer);
  if (ms) toastTimer = setTimeout(() => (t.hidden = true), ms);
}

// Keyboard shortcuts (handy with a Magic Keyboard).
document.addEventListener('keydown', (e) => {
  const mod = e.metaKey || e.ctrlKey;
  if (mod && e.key.toLowerCase() === 'z') {
    e.preventDefault();
    e.shiftKey ? doRedo() : doUndo();
  } else if (e.key === 'ArrowRight' || e.key === 'PageDown') gotoPage((doc?.page ?? 0) + 1);
  else if (e.key === 'ArrowLeft' || e.key === 'PageUp') gotoPage((doc?.page ?? 0) - 1);
  else if (e.key === 'e') setTool('eraser');
  else if (e.key === 'p') setTool('pen');
  else if (e.key === 'h') setTool('hl');
});

// ---------------------------------------------------------------- layout

function layout() {
  surface.resize();
  if (doc) showPage(!zoomed);
}
window.addEventListener('resize', layout);
syncUndoButtons();
layout();
