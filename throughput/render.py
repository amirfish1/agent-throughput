"""Terminal rendering for ``throughput analyze``: colour, bars and wrapping, stdlib only.

Colour is on only when stdout is a terminal and ``NO_COLOR`` is unset
(``FORCE_COLOR`` turns it on anyway). Without colour the layout is the same,
so piped output stays readable.
"""

from __future__ import annotations

import os
import shutil
import sys
import textwrap

_CODES = {"bold": "1", "dim": "2", "red": "31", "green": "32", "yellow": "33", "blue": "34",
          "magenta": "35", "cyan": "36", "grey": "90"}
_EIGHTHS = " ▏▎▍▌▋▊▉"


def color_enabled(stream=None) -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    stream = stream or sys.stdout
    return hasattr(stream, "isatty") and stream.isatty() and os.environ.get("TERM") != "dumb"


class Style:
    def __init__(self, enabled: bool):
        self.enabled = enabled

    def __call__(self, text, *styles) -> str:
        if not self.enabled or not styles:
            return str(text)
        return f"\033[{';'.join(_CODES[s] for s in styles)}m{text}\033[0m"


def width() -> int:
    return max(60, min(100, shutil.get_terminal_size((90, 24)).columns))


def bar(fraction: float, cells: int) -> str:
    """A left-aligned bar ``cells`` wide, drawn to 1/8 of a cell."""
    fraction = min(max(fraction, 0.0), 1.0)
    eighths = round(fraction * cells * 8)
    full, part = divmod(eighths, 8)
    s = "█" * full + (_EIGHTHS[part] if part else "")
    return s.ljust(cells)


def gauge(fraction: float, cells: int) -> tuple:
    """(filled, empty) halves of a score gauge."""
    filled = round(min(max(fraction, 0.0), 1.0) * cells)
    return "█" * filled, "░" * (cells - filled)


def score_color(score: int) -> str:
    return "green" if score >= 80 else "yellow" if score >= 50 else "red"


def wrap(text: str, indent: int, total: int) -> list:
    return textwrap.wrap(text, width=max(30, total - indent)) or [""]


def rule(title: str, total: int, st: Style) -> str:
    return st(title.upper(), "bold", "cyan") + " " + st("─" * max(0, total - len(title) - 1), "grey")
