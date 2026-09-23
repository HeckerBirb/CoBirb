"""`python -m snake`: the game in a terminal, on its own clock."""
from __future__ import annotations

import curses
import time

from .engine import Game
from .loop import Loop
from .render import render

TITLE = [
    r"  ___  _ __   __ _| | _____ ",
    r" / __|| '_ \ / _` | |/ / _ \\",
    r" \__ \| | | | (_| |   <  __/",
    r" |___/|_| |_|\__,_|_|\_\___|",
]


def main(screen) -> None:
    curses.curs_set(0)
    screen.nodelay(True)
    height, width = screen.getmaxyx()
    game = Game(max(10, width - 4), max(6, height - 8))
    loop = Loop(game, 0.1)
    last = time.monotonic()
    while True:
        keys = []
        key = screen.getch()
        while key != -1:
            if key in (ord("q"), 27):
                return
            keys.append(key)
            key = screen.getch()
        now = time.monotonic()
        loop.advance(now - last, keys)
        last = now
        screen.erase()
        for row, line in enumerate(TITLE):
            screen.addstr(row, 0, line[: width - 1])
        for row, line in enumerate(render(game)):
            screen.addstr(len(TITLE) + row, 0, line[: width - 1])
        status = f" score {game.score}" + ("   GAME OVER — q to quit" if game.over else "")
        screen.addstr(min(height - 1, len(TITLE) + game.height + 2), 0, status[: width - 1])
        screen.refresh()
        time.sleep(1 / 60)


if __name__ == "__main__":
    curses.wrapper(main)
