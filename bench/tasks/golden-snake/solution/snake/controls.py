"""Keys to directions."""
from __future__ import annotations

import curses

_KEYS = {
    curses.KEY_UP: "up", curses.KEY_DOWN: "down", curses.KEY_LEFT: "left", curses.KEY_RIGHT: "right",
    "w": "up", "a": "left", "s": "down", "d": "right",
    "k": "up", "h": "left", "j": "down", "l": "right",
}


def key_to_direction(key):
    if isinstance(key, int) and key not in _KEYS and 0 <= key < 0x110000:
        key = chr(key)
    return _KEYS.get(key)
