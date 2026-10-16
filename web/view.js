import { Surface, applyOp, connect, exportPDF, indexDoc, uploadPDF } from '/ink.js';

const $ = (s) => document.querySelector(s);
const stage = $('#stage');
const key = new URLSearchParams(location.search).get('k') || '';
const surface = new Surface(stage, key);

let doc = null;
let idx = null;
let pads = 0;
let pairForced = null; // null = automatic, true/false = user toggled
let cursor = null; // {page, x, y, t}

const curPage = () => doc && doc.pages[doc.page];

const net = connect('view', {
  status(ok) {
    $('#status').className = ok ? 'ok' : 'bad';
    $('#status').title = ok ? 'Connected' : 'Reconnecting…';
  },
  message(msg) {
    if (msg.t === 'state') {
      doc = msg.doc;
      idx = indexDoc(doc);
      pads = msg.pads;
      showPage();
      return syncPair();
    }
    if (msg.t === 'peers') {
      pads = msg.pads;
      return syncPair();
    }
    if (!doc) return;
    if (msg.t === 'cursor') {
      cursor = msg.hide ? null : { page: msg.page, x: msg.x, y: msg.y, t: performance.now() };
      return surface.queueLive();
    }
    const before = doc.page;
    const page = applyOp(doc, idx, msg);
    if (msg.t === 'goto' && doc.page !== before) return showPage();
    const onPage = page && page === curPage();
    if (msg.t === 'begin' && onPage) {
      surface.active.set(msg.s.id, idx.strokes.get(msg.s.id));
      surface.queueLive();
    }
    if (msg.t === 'pts' && surface.active.has(msg.id)) surface.queueLive();
    if (msg.t === 'end') {
      const s = idx.strokes.get(msg.id);
      if (s && surface.active.has(s.id)) surface.commit(s);
    }
    if ((msg.t === 'remove' || msg.t === 'restore') && onPage) surface.drawInk();
    if (msg.t === 'paper' && onPage) surface.drawBg();
    if (msg.t !== 'pts') syncPair();
  },
});

// Pen tip / hover cursor so viewers can follow along.
surface.overlay = (ctx, k) => {
  const page = curPage();
  let at = null;
  for (const s of surface.active.values()) {
    const p = s.pts[s.pts.length - 1];
    if (p) at = [p[0], p[1], s.color];
  }
  if (!at && cursor && cursor.page === page?.id) at = [cursor.x, cursor.y, null];
  if (!at) return;
  ctx.save();
  ctx.beginPath();
  ctx.arc(at[0] * k, at[1] * k, Math.max(4, 5 * k), 0, Math.PI * 2);
  ctx.fillStyle = 'rgba(230, 60, 60, 0.35)';
  ctx.strokeStyle = 'rgba(230, 60, 60, 0.9)';
  ctx.lineWidth = 1.5;
  ctx.fill();
  ctx.stroke();
  ctx.restore();
};

function showPage() {
  surface.active.clear();
  surface.fit(curPage(), 24);
  surface.setPage(curPage());
  $('#pageno').textContent = `${doc.page + 1} / ${doc.pages.length}`;
}

function gotoPage(i) {
  if (!doc || i < 0 || i >= doc.pages.length) return;
  doc.page = i;
  net.send({ t: 'goto', index: i });
  showPage();
}

// ---------------------------------------------------------------- pairing card

let infoLoaded = false;
async function loadInfo() {
  if (infoLoaded) return;
  try {
    const r = await fetch('/api/info');
    if (!r.ok) return;
    const info = await r.json();
    const url = info.urls[0];
    $('#url').textContent = url || 'No Wi-Fi address found. Connect to a network and restart Inkcast.';
    const qr = $('#qr');
    qr.onerror = () => (qr.hidden = true);
    if (url) qr.src = '/api/qr.svg?' + Date.now();
    else qr.hidden = true;
    infoLoaded = true;
  } catch {}
}

function syncPair() {
  const empty = doc && doc.pages.every((p) => !p.strokes.length) && !doc.pdf;
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

function fullscreen() {
  if (document.fullscreenElement) document.exitFullscreen();
  else document.documentElement.requestFullscreen().catch(() => {});
}

$('#b-prev').onclick = () => gotoPage((doc?.page ?? 0) - 1);
$('#b-next').onclick = () => gotoPage((doc?.page ?? 0) + 1);
$('#b-qr').onclick = togglePair;
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
  else if (k === 'arrowright' || k === 'pagedown' || k === ' ') gotoPage((doc?.page ?? 0) + 1);
  else if (k === 'arrowleft' || k === 'pageup') gotoPage((doc?.page ?? 0) - 1);
  else if (k === 'escape' && !$('#pair').hidden) togglePair();
});

// Hide the controls when the mouse is idle so screen shares stay clean.
let idleTimer = 0;
function wake() {
  document.body.classList.add('awake');
  clearTimeout(idleTimer);
  idleTimer = setTimeout(() => document.body.classList.remove('awake'), 2200);
}
document.addEventListener('mousemove', wake);

// Drag and drop a PDF onto the window.
let dragDepth = 0;
document.addEventListener('dragenter', (e) => {
  e.preventDefault();
  dragDepth++;
  $('#drop').hidden = false;
});
document.addEventListener('dragleave', () => {
  if (--dragDepth <= 0) $('#drop').hidden = true;
});
document.addEventListener('dragover', (e) => e.preventDefault());
document.addEventListener('drop', (e) => {
  e.preventDefault();
  dragDepth = 0;
  $('#drop').hidden = true;
  const f = [...e.dataTransfer.files].find((f) => f.type === 'application/pdf' || /\.pdf$/i.test(f.name));
  if (f) openPDF(f);
  else toast('That is not a PDF');
});

function layout() {
  surface.resize();
  if (doc) showPage();
}
window.addEventListener('resize', layout);
layout();
