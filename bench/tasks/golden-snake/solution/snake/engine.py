"""The rules: a board, a snake, food, and one tick at a time."""
from __future__ import annotations

import random

_VECTORS = {"up": (0, -1), "down": (0, 1), "left": (-1, 0), "right": (1, 0)}
_OPPOSITE = {"up": "down", "down": "up", "left": "right", "right": "left"}
POINTS = 10
MAX_QUEUED = 2


class Game:
    def __init__(self, width: int, height: int, seed: int = 0) -> None:
        self.width, self.height = width, height
        self._rng = random.Random(seed)
        head = (width // 2, height // 2)
        self.snake = [(head[0] - i, head[1]) for i in range(3)]
        self.direction = "right"
        self._queue: list[str] = []
        self.score = 0
        self.over = False
        self.food = self._place_food()

    def _place_food(self):
        taken = set(self.snake)
        free = [(x, y) for y in range(self.height) for x in range(self.width) if (x, y) not in taken]
        return free[self._rng.randrange(len(free))] if free else None

    def turn(self, direction: str) -> None:
        if direction not in _VECTORS or len(self._queue) >= MAX_QUEUED:
            return
        last = self._queue[-1] if self._queue else self.direction
        if direction in (last, _OPPOSITE[last]):
            return
        self._queue.append(direction)

    def tick(self) -> None:
        if self.over:
            return
        if self._queue:
            self.direction = self._queue.pop(0)
        dx, dy = _VECTORS[self.direction]
        x, y = self.snake[0]
        head = (x + dx, y + dy)
        if not (0 <= head[0] < self.width and 0 <= head[1] < self.height):
            self.over = True
            return
        eating = head == self.food
        body = self.snake if eating else self.snake[:-1]
        if head in body:
            self.over = True
            return
        self.snake = [head] + body
        if eating:
            self.score += POINTS
            self.food = self._place_food()
            if self.food is None:
                self.over = True
