import pytest

from snake.engine import Game
from snake.render import render


@pytest.mark.parametrize("width, height", [(10, 8), (20, 5), (6, 6)])
def test_the_frame_has_the_exact_size(width, height):
    frame = render(Game(width, height))
    assert len(frame) == height + 2
    assert all(len(line) == width + 2 for line in frame)


def test_the_board_has_a_border():
    frame = render(Game(10, 8))
    assert " " not in frame[0] and " " not in frame[-1]
    assert all(line[0] != " " and line[-1] != " " for line in frame)


def _at(frame, cell):
    x, y = cell
    return frame[y + 1][x + 1]


def test_the_head_body_and_food_are_drawn():
    game = Game(10, 8)
    game.food = (1, 1)
    frame = render(game)
    head, body = _at(frame, game.snake[0]), _at(frame, game.snake[1])
    assert head != " " and body != " " and head != body
    assert _at(frame, game.food) not in (" ", head, body)


def test_empty_cells_are_spaces():
    game = Game(10, 8)
    game.food = (1, 1)
    assert _at(render(game), (8, 6)) == " "
