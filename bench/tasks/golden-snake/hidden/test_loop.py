import curses

import pytest

from snake.engine import Game
from snake.loop import Loop


def _loop():
    game = Game(20, 10)
    game.food = (0, 0)
    return game, Loop(game, 0.1)


@pytest.mark.parametrize("steps, ticks", [
    ([0.05], [0]),
    ([0.05, 0.06], [0, 1]),       # unused time carries over
    ([0.35], [3]),
    ([0.0, 0.0, 0.1], [0, 0, 1]),
])
def test_ticks_follow_the_clock(steps, ticks):
    _, loop = _loop()
    assert [loop.advance(elapsed, []) for elapsed in steps] == ticks


def test_keys_never_cause_a_tick():
    game, loop = _loop()
    head = game.snake[0]
    assert loop.advance(0.0, [curses.KEY_UP, "a"]) == 0
    assert game.snake[0] == head


def test_keys_steer_the_next_tick():
    game, loop = _loop()
    x, y = game.snake[0]
    loop.advance(0.0, [curses.KEY_UP])
    loop.advance(0.1, [])
    assert game.snake[0] == (x, y - 1)
