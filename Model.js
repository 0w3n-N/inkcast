.pragma library

// Pure helpers for Panel.qml. Everything read from disk or from a process is
// treated as untrusted: parsed defensively, bounded, and reduced to plain values.

var STALE_AFTER_SEC = 25

function singleLine(value, limit) {
  return String(value === undefined || value === null ? "" : value).replace(/[\r\n\t]+/g, " ").slice(0, limit)
}

function toInt(value, lo, hi, fallback) {
  var n = Math.round(Number(value))
  return isFinite(n) ? Math.max(lo, Math.min(hi, n)) : fallback
}

// status.json written by the server (see inkcast/server.py write_status).
function parseStatus(raw, nowSec) {
  var empty = { running: false, pads: 0, views: 0, url: "", qr: [], qrSize: 0, page: 0, pages: 0, port: 0, live: "idle" }
  if (typeof raw !== "string" || raw.length === 0 || raw.length > 200000) return empty
  var s
  try { s = JSON.parse(raw) } catch (e) { return empty }
  if (!s || typeof s !== "object" || s.app !== "inkcast") return empty
  var updated = Number(s.updated)
  var fresh = isFinite(updated) && Math.abs(nowSec - updated) < STALE_AFTER_SEC
  var qr = []
  if (Array.isArray(s.qr) && s.qr.length >= 21 && s.qr.length <= 200) {
    var size = s.qr.length
    for (var i = 0; i < size; i++) {
      var row = s.qr[i]
      if (typeof row !== "string" || row.length !== size || !/^[01]+$/.test(row)) { qr = []; break }
      qr.push(row)
    }
  }
  var url = singleLine(s.url, 300)
  if (!/^http:\/\/[A-Za-z0-9.\-\[\]:]+\/\?k=[A-Za-z0-9_-]+$/.test(url)) url = ""
  return {
    running: s.running === true && fresh,
    pads: toInt(s.pads, 0, 99, 0),
    views: toInt(s.views, 0, 99, 0),
    url: url,
    qr: qr,
    qrSize: qr.length,
    page: toInt(s.page, 0, 100000, 0),
    pages: toInt(s.pages, 0, 100000, 0),
    port: toInt(s.port, 0, 65535, 0),
    live: s.running === true && fresh && (s.live === "picking" || s.live === "live") ? s.live : "idle"
  }
}

// `inkcast doctor --json`.
function parseDoctor(raw) {
  var d = { known: false, uxplay: false, poppler: false, avahi: false, capture: false, firewall: "unknown", lan: "" }
  if (typeof raw !== "string" || raw.length > 10000) return d
  try {
    var j = JSON.parse(raw)
    d.known = true
    d.uxplay = j.uxplay === true
    d.poppler = j.poppler === true
    d.avahi = j.avahi === true
    d.capture = j.capture === true
    d.firewall = ["open", "configured", "needs-setup"].indexOf(j.firewall) >= 0 ? j.firewall : "unknown"
    d.lan = singleLine(j.lan, 64)
  } catch (e) {}
  return d
}

function needsSetup(d) {
  return d.known && (!d.poppler || !d.capture || d.firewall === "needs-setup")
}

function statusLine(st, mirroring) {
  if (!st.running) return mirroring ? "Mirroring on" : "Off"
  if (st.pads > 0) return st.pads === 1 ? "iPad connected" : st.pads + " iPads connected"
  return "Waiting for iPad"
}

function detailLine(st) {
  if (!st.running) return ""
  var parts = [st.pages === 1 ? "1 page" : st.pages + " pages"]
  parts.push(st.views === 1 ? "1 window open" : st.views + " windows open")
  return parts.join(" · ")
}

// Quote one argument for the floating-terminal launcher, which runs `bash -c`.
function shellQuote(s) {
  return "'" + String(s).replace(/'/g, "'\\''") + "'"
}

function parseMirror(raw) {
  try {
    var j = JSON.parse(String(raw || ""))
    return { name: singleLine(j.name, 40), pin: /^\d{4}$/.test(j.pin) ? j.pin : "" }
  } catch (e) {
    return { name: "", pin: "" }
  }
}

function airplayName(value) {
  var name = singleLine(value, 40).replace(/[^\w .'-]/g, "").trim()
  return name.length > 0 ? name : "Omarchy"
}

function urlForDisplay(url) {
  // Show the address without the key; the key is in the QR code.
  return String(url || "").replace(/\?k=.*$/, "")
}
