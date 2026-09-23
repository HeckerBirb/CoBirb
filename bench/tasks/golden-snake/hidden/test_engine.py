import pytest

from snake.engine import Game


def test_a_new_game():
    game = Game(10, 8, seed=1)
    assert game.snake == [(5, 4), (4, 4), (3, 4)]
    assert game.score == 0 and game.over is False
    assert game.food is not None and game.food not in game.snake


@pytest.mark.parametrize("seed", range(50))
def test_food_is_never_on_the_snake(seed):
    game = Game(6, 4, seed=seed)
    for _ in range(3):
        assert game.food not in game.snake
        game.food = (game.snake[0][0] + 1, game.snake[0][1])
        game.tick()


@pytest.mark.parametrize("turns, head", [
    ([], (6, 4)),
    (["up"], (5, 3)),
    (["down"], (5, 5)),
    (["left"], (6, 4)),          # a reversal is ignored
    (["right"], (6, 4)),         # so is the current direction
    (["up", "left"], (5, 3)),    # only the oldest turn applies per tick
])
def test_one_tick(turns, head):
    game = Game(10, 8)
    game.food = (0, 0)
    for turn in turns:
        game.turn(turn)
    game.tick()
    assert game.snake[0] == head and len(game.snake) == 3


def test_two_quick_turns_are_both_kept():
    game = Game(10, 8)
    game.food = (0, 0)
    game.turn("up")
    game.turn("left")
    game.tick()
    game.tick()
    assert game.snake[0] == (4, 3)


def test_a_third_queued_turn_is_dropped():
    game = Game(10, 8)
    game.food = (0, 0)
    for turn in ("up", "left", "down"):
        game.turn(turn)
    for _ in range(3):
        game.tick()
    assert game.snake[0] == (3, 3)


def test_eating_grows_the_snake_and_scores():
    game = Game(10, 8)
    game.food = (6, 4)
    game.tick()
    assert game.snake == [(6, 4), (5, 4), (4, 4), (3, 4)]
    assert game.score == 10
    assert game.food is not None and game.food not in game.snake


@pytest.mark.parametrize("width, height, turn", [(10, 8, None), (10, 8, "up"), (10, 8, "down")])
def test_leaving_the_board_ends_the_game(width, height, turn):
    game = Game(width, height)
    game.food = (0, 0)
    if turn:
        game.turn(turn)
    for _ in range(12):
        game.tick()
    assert game.over is True


def test_hitting_the_body_ends_the_game():
    game = Game(10, 8)
    game.snake = [(5, 4), (5, 5), (4, 5), (4, 4), (3, 4)]
    game.food = (0, 0)
    game.turn("down")
    game.tick()
    game.turn("left")
    game.tick()
    assert game.over is True


def test_moving_into_the_leaving_tail_is_allowed():
    game = Game(10, 8)
    # A ring: the tail (5, 4) is right of the head, which is moving right.
    game.snake = [(4, 4), (4, 5), (5, 5), (5, 4)]
    game.food = (0, 0)
    game.tick()
    assert game.over is False and game.snake[0] == (5, 4)


def test_nothing_changes_after_the_game_is_over():
    game = Game(4, 3)
    game.food = (0, 0)
    for _ in range(5):
        game.tick()
    assert game.over is True
    before = (list(game.snake), game.score, game.food)
    game.tick()
    assert (game.snake, game.score, game.food) == before
