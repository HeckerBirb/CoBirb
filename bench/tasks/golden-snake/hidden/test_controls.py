import curses

import pytest

from snake.controls import key_to_direction


@pytest.mark.parametrize("key, direction", [
    (curses.KEY_UP, "up"), (curses.KEY_DOWN, "down"),
    (curses.KEY_LEFT, "left"), (curses.KEY_RIGHT, "right"),
    ("w", "up"), ("a", "left"), ("s", "down"), ("d", "right"),
    ("k", "up"), ("h", "left"), ("j", "down"), ("l", "right"),
    (ord("w"), "up"), (ord("l"), "right"),
    ("x", None), (ord("q"), None), (-1, None),
])
def test_keys_map_to_directions(key, direction):
    assert key_to_direction(key) == direction
