#!/usr/bin/env python3
"""Generate the Inkcast icons.

The mark is a penguin whose body is an apple and whose belly is a tablet
screen, with a stylus writing on it: Linux + iPad + Pencil in one shape.
The apple is a generic fruit (no bite), not Apple's logo.

Outputs (in assets/):
  icon.svg        full-color app icon (also copied to web/icon.svg)
  icon-mono.svg   single-path glyph, used for the bar icon (InkcastIcon.qml)
  icon-256/512.png raster sizes (web/icon.png is the 180px iOS home-screen icon)

Requires Inkscape (for the boolean path ops) and rsvg-convert.
Run: python3 assets/build-icons.py
"""

import math
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

# ----------------------------------------------------------------- geometry (24x24 grid)

BODY = ("M10.5 7.2 C9.2 6.1 7.4 5.7 5.9 6.3 C3.4 7.3 2.2 10.1 2.5 13.2 "
        "C2.9 17 5.3 20.9 7.9 21.1 C9 21.2 9.6 20.6 10.5 20.6 "
        "C11.4 20.6 12 21.2 13.1 21.1 C15.7 20.9 18.1 17 18.5 13.2 "
        "C18.8 10.1 17.6 7.3 15.1 6.3 C13.6 5.7 11.8 6.1 10.5 7.2 Z")
LEAF = "M10.9 6.5 C10.6 4.2 12.1 2.3 14.6 2 C14.9 4.4 13.4 6.2 10.9 6.5 Z"
SCREEN = (7.0, 10.7, 7.0, 7.9, 1.1)  # x, y, w, h, r — the belly is a tablet screen
EYES = [(8.85, 8.75), (12.15, 8.75)]
EYE_R = 1.0
FEET = [(6.9, 21.35, -8), (14.1, 21.35, 8)]  # cx, cy, rotation
FOOT_RX, FOOT_RY = 2.25, 1.0
BEAK = "M9.75 9.55 L11.25 9.55 L10.5 10.45 Z"

TIP = (12.1, 15.1)
DIR = (0.671, -0.741)  # from the tip toward the end of the pen
PEN_W = 2.6
CONE = 2.5
GAP = 0.45
LENGTH = 12.6
KNOCKOUT = 0.95  # clear space around the pen where it crosses the body

INK = "M8.2 16.9 C8.9 15.6 9.5 15.6 9.9 16.5 C10.3 17.4 10.9 17.3 11.4 16"


def pen_points(extra=0.0):
    ux, uy = DIR
    nx, ny = -uy, ux
    hw = PEN_W / 2 + extra
    tx, ty = TIP

    def at(s, off):
        return (tx + ux * s + nx * off, ty + uy * s + ny * off)

    return {
        "tip": at(-extra, 0),
        "cone_l": at(CONE, -hw), "cone_r": at(CONE, hw),
        "shaft_l0": at(CONE + GAP, -hw), "shaft_r0": at(CONE + GAP, hw),
        "end_l": at(LENGTH + extra, -hw), "end_r": at(LENGTH + extra, hw),
        "band_l": at(CONE + GAP + 1.3, -hw), "band_r": at(CONE + GAP + 1.3, hw),
    }


def fmt(p):
    return f"{p[0]:.3f} {p[1]:.3f}"


def pen_cone():
    p = pen_points()
    return f"M{fmt(p['tip'])} L{fmt(p['cone_r'])} L{fmt(p['cone_l'])} Z"


def pen_shaft():
    p = pen_points()
    return f"M{fmt(p['shaft_l0'])} L{fmt(p['end_l'])} L{fmt(p['end_r'])} L{fmt(p['shaft_r0'])} Z"


def pen_hull(extra):
    p = pen_points(extra)
    return f"M{fmt(p['tip'])} L{fmt(p['cone_r'])} L{fmt(p['end_r'])} L{fmt(p['end_l'])} L{fmt(p['cone_l'])} Z"


def ellipse_path(cx, cy, rx, ry, rot=0):
    pts = []
    for i in range(32):
        a = 2 * math.pi * i / 32
        x, y = rx * math.cos(a), ry * math.sin(a)
        c, s = math.cos(math.radians(rot)), math.sin(math.radians(rot))
        pts.append((cx + x * c - y * s, cy + x * s + y * c))
    return "M" + " L".join(fmt(p) for p in pts) + " Z"


def rrect(x, y, w, h, r):
    return (f"M{x + r} {y} H{x + w - r} A{r} {r} 0 0 1 {x + w} {y + r} V{y + h - r} "
            f"A{r} {r} 0 0 1 {x + w - r} {y + h} H{x + r} A{r} {r} 0 0 1 {x} {y + h - r} "
            f"V{y + r} A{r} {r} 0 0 1 {x + r} {y} Z")


def circle(cx, cy, r):
    return ellipse_path(cx, cy, r, r)


# ----------------------------------------------------------------- inkscape booleans


def inkscape_op(paths, op):
    """Combine paths with an Inkscape boolean op; returns one path `d`."""
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "in.svg"
        out = Path(tmp) / "out.svg"
        body = "".join(f'<path id="p{i}" d="{d}"/>' for i, d in enumerate(paths))
        src.write_text(f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="24" height="24">{body}</svg>')
        actions = f"select-all;path-{op};export-filename:{out};export-plain-svg;export-do"
        subprocess.run(["inkscape", str(src), f"--actions={actions}"], check=True,
                       capture_output=True)
        m = re.search(r'\sd="([^"]+)"', out.read_text())
        return m.group(1)


def mono_glyph():
    solid = inkscape_op([BODY, LEAF] + [ellipse_path(x, y, FOOT_RX, FOOT_RY, r) for x, y, r in FEET], "union")
    holes = inkscape_op([rrect(*SCREEN)] + [circle(x, y, EYE_R) for x, y in EYES] + [pen_hull(KNOCKOUT)], "union")
    cut = inkscape_op([solid, holes], "difference")
    # No ink squiggle here: at bar size it only adds noise.
    return inkscape_op([cut, pen_cone(), pen_shaft()], "union")


def stroke_to_fill(d, width):
    """Turn a stroked line into a filled outline (via Inkscape)."""
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "in.svg"
        out = Path(tmp) / "out.svg"
        src.write_text('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="24" height="24">'
                       f'<path d="{d}" fill="none" stroke="#000" stroke-width="{width}" stroke-linecap="round"/></svg>')
        actions = f"select-all;object-stroke-to-path;export-filename:{out};export-plain-svg;export-do"
        subprocess.run(["inkscape", str(src), f"--actions={actions}"], check=True, capture_output=True)
        return re.search(r'\sd="([^"]+)"', out.read_text()).group(1)


# ----------------------------------------------------------------- outputs


def color_svg(glyph_cut):
    """Full-color icon on a rounded tile (viewBox 0 0 64 64, mark scaled from 24)."""
    s = 2.1  # mark scale inside the tile
    off = (64 - 24 * s) / 2
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
  <defs>
    <linearGradient id="tile" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#4f8cff"/>
      <stop offset="1" stop-color="#2b5bd7"/>
    </linearGradient>
    <linearGradient id="screen" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#ffffff"/>
      <stop offset="1" stop-color="#e9eef7"/>
    </linearGradient>
  </defs>
  <rect width="64" height="64" rx="14" fill="url(#tile)"/>
  <g transform="translate({off:.2f} {off + 0.6:.2f}) scale({s})">
    <path d="{LEAF}" fill="#5dd37a"/>
    {"".join(f'<path d="{ellipse_path(x, y, FOOT_RX, FOOT_RY, r)}" fill="#ffb02e"/>' for x, y, r in FEET)}
    <path d="{glyph_cut}" fill="#14161b"/>
    <path d="{rrect(*SCREEN)}" fill="url(#screen)"/>
    {"".join(f'<circle cx="{x}" cy="{y}" r="{EYE_R}" fill="#fff"/><circle cx="{x + 0.25}" cy="{y + 0.15}" r="0.45" fill="#14161b"/>' for x, y in EYES)}
    <path d="{BEAK}" fill="#ffb02e"/>
    <path d="{INK}" fill="none" stroke="#2b5bd7" stroke-width="0.85" stroke-linecap="round"/>
    <path d="{pen_shaft()}" fill="#f4f5f8"/>
    <path d="{pen_cone()}" fill="#c9ced8"/>
  </g>
</svg>
'''


def main():
    if not shutil.which("inkscape") or not shutil.which("rsvg-convert"):
        raise SystemExit("needs inkscape and rsvg-convert")
    glyph = mono_glyph()
    (HERE / "icon-mono.svg").write_text(
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><path d="{glyph}"/></svg>\n')

    # Color version: body with only the knockout cut (screen/eyes are drawn on top).
    solid = inkscape_op([BODY], "union")
    cut = inkscape_op([solid, pen_hull(KNOCKOUT * 0.7)], "difference")
    svg = color_svg(cut)
    (HERE / "icon.svg").write_text(svg)
    shutil.copy(HERE / "icon.svg", ROOT / "web" / "icon.svg")

    for size in (256, 512):
        subprocess.run(["rsvg-convert", "-w", str(size), "-h", str(size), str(HERE / "icon.svg"),
                        "-o", str(HERE / f"icon-{size}.png")], check=True)
    subprocess.run(["rsvg-convert", "-w", "180", "-h", "180", str(HERE / "icon.svg"),
                    "-o", str(ROOT / "web" / "icon.png")], check=True)
    # Windows Start menu shortcut icon.
    if shutil.which("magick"):
        subprocess.run(["magick", "-background", "none", str(HERE / "icon.svg"),
                        "-define", "icon:auto-resize=256,64,48,32,16", str(HERE / "icon.ico")], check=True)
    # The QML glyph uses the same path.
    qml = ROOT / "InkcastIcon.qml"
    if qml.exists():
        text = qml.read_text()
        text = re.sub(r'(readonly property string glyph: ")[^"]*(")', lambda m: m.group(1) + glyph + m.group(2), text)
        qml.write_text(text)
    print("icons written")


if __name__ == "__main__":
    main()
