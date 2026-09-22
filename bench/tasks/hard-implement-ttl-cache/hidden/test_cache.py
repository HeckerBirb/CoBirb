from cache import ttl_cache


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def make(clock):
    calls = []

    @ttl_cache(10, clock=clock)
    def square(x, *, offset=0):
        """Square."""
        calls.append((x, offset))
        return x * x + offset

    return square, calls


def test_caches_within_ttl():
    clock = Clock(); square, calls = make(clock)
    assert square(3) == 9 and square(3) == 9
    assert calls == [(3, 0)]


def test_expires():
    clock = Clock(); square, calls = make(clock)
    square(3); clock.now = 11; square(3)
    assert len(calls) == 2


def test_keyword_order_and_distinct_args():
    clock = Clock(); square, calls = make(clock)
    square(2, offset=1); square(2, offset=1); square(2, offset=2)
    assert len(calls) == 2


def test_exceptions_are_not_cached():
    clock = Clock(); attempts = []

    @ttl_cache(10, clock=clock)
    def flaky():
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("first")
        return "ok"

    try:
        flaky()
    except RuntimeError:
        pass
    assert flaky() == "ok" and len(attempts) == 2


def test_clear_size_and_wraps():
    clock = Clock(); square, _ = make(clock)
    square(1); square(2)
    assert square.cache_size() == 2
    square.cache_clear()
    assert square.cache_size() == 0
    assert square.__name__ == "square" and square.__doc__ == "Square."
