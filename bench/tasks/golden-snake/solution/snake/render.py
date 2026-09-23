"""The frame as text: a border, the snake and the food."""
from __future__ import annotations

HEAD, BODY, FOOD = "@", "o", "*"


def render(game) -> list[str]:
    rows = [[" "] * game.width for _ in range(game.height)]
    for x, y in game.snake[1:]:
        rows[y][x] = BODY
    if game.food is not None:
        fx, fy = game.food
        rows[fy][fx] = FOOD
    hx, hy = game.snake[0]
    rows[hy][hx] = HEAD
    edge = "+" + "-" * game.width + "+"
    return [edge] + ["|" + "".join(row) + "|" for row in rows] + [edge]
