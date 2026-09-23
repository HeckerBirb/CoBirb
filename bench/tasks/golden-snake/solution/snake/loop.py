"""The fixed-timestep loop, without the terminal."""
from __future__ import annotations

from .controls import key_to_direction


class Loop:
    def __init__(self, game, tick_seconds: float) -> None:
        self.game = game
        self.tick_seconds = tick_seconds
        self._carry = 0.0

    def advance(self, elapsed: float, keys) -> int:
        for key in keys:
            direction = key_to_direction(key)
            if direction:
                self.game.turn(direction)
        self._carry += elapsed
        ticks = 0
        while self._carry + 1e-9 >= self.tick_seconds:
            self._carry -= self.tick_seconds
            self.game.tick()
            ticks += 1
        return ticks
