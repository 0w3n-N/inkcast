"""Match the web pages to the active Omarchy theme.

Reads ~/.local/state/omarchy/current/theme/colors.toml and turns it into CSS
custom properties. Only `#rrggbb` values and a sanitized font name make it
into the stylesheet, so a theme file can't inject CSS. Without Omarchy (or on
macOS) the stylesheet is empty and the built-in palette applies.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
from pathlib import Path

HEX = re.compile(r'^\s*([a-z_]+)\s*=\s*"(#[0-9a-fA-F]{6})"\s*$')
MODE = re.compile(r'^\s*mode\s*=\s*"(light|dark)"\s*$')


def _rgb(h):
    return tuple(int(h[i:i + 2], 16) for i in (1, 3, 5))


def _hex(rgb):
    return "#" + "".join(f"{max(0, min(255, round(c))):02x}" for c in rgb)


def mix(a, b, t):
    """Blend color a toward b by t (0..1)."""
    ra, rb = _rgb(a), _rgb(b)
    return _hex(tuple(x + (y - x) * t for x, y in zip(ra, rb)))


class Theme:
    def __init__(self):
        state = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
        self.dir = Path(state) / "omarchy" / "current" / "theme"
        self.version = ""
        self._css = ""
        self._sig = None
        self.changed()

    def _signature(self):
        parts = []
        for f in (self.dir / "colors.toml", self.dir.parent / "theme.name"):
            try:
                st = f.stat()
                parts.append(f"{f}:{st.st_mtime_ns}:{st.st_size}")
            except OSError:
                parts.append(f"{f}:-")
        return "|".join(parts)

    def changed(self) -> bool:
        """Reload if the theme changed. Returns True when it did."""
        sig = self._signature()
        if sig == self._sig:
            return False
        first = self._sig is None
        self._sig = sig
        self._css = self._build()
        self.version = hashlib.sha1(self._css.encode()).hexdigest()[:10]
        return not first

    def css(self) -> str:
        return self._css

    def _build(self) -> str:
        try:
            text = (self.dir / "colors.toml").read_text(errors="replace")
        except OSError:
            return ""
        colors, mode = {}, "dark"
        for line in text.splitlines():
            m = HEX.match(line)
            if m:
                colors[m.group(1)] = m.group(2).lower()
            m = MODE.match(line)
            if m:
                mode = m.group(1)
        bg, fg = colors.get("background"), colors.get("foreground")
        if not bg or not fg:
            return ""
        accent = colors.get("accent", colors.get("blue", fg))
        props = {
            "bg": bg,
            "panel": mix(bg, fg, 0.06),
            "panel-2": mix(bg, fg, 0.12),
            "line": mix(bg, fg, 0.22),
            "fg": fg,
            "muted": mix(bg, fg, 0.6),
            "accent": accent,
            "ok": colors.get("green", accent),
            "bad": colors.get("red", accent),
            "letterbox": colors.get("dark_background", mix(bg, "#000000", 0.25 if mode == "dark" else 0.06)),
        }
        lines = [f"  --{k}: {v};" for k, v in props.items()]
        lines.append(f"  --radius: {self._radius()}px;")
        font = self._font()
        if font:
            lines.append(f'  --font: "{font}", -apple-system, BlinkMacSystemFont, system-ui, sans-serif;')
        lines.append(f"  color-scheme: {mode};")
        return ":root {\n" + "\n".join(lines) + "\n}\n"

    @staticmethod
    def _font():
        if not shutil.which("omarchy"):
            return ""
        try:
            out = subprocess.run(["omarchy", "font", "current"], capture_output=True, text=True, timeout=5).stdout
        except (OSError, subprocess.SubprocessError):
            return ""
        name = out.strip().splitlines()[0] if out.strip() else ""
        return name if re.fullmatch(r"[A-Za-z0-9 _.-]{1,64}", name) else ""

    @staticmethod
    def _radius():
        if not shutil.which("hyprctl"):
            return 10
        try:
            out = subprocess.run(["hyprctl", "getoption", "decoration:rounding", "-j"],
                                 capture_output=True, text=True, timeout=3).stdout
            m = re.search(r'"int"\s*:\s*(\d+)', out)
            return min(int(m.group(1)), 16) if m else 10
        except (OSError, subprocess.SubprocessError, ValueError):
            return 10
