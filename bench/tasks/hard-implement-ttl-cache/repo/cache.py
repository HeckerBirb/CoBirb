import time


def ttl_cache(seconds, clock=time.monotonic):
    """Cache a function's results for `seconds`.

    - Results are cached per argument tuple (positional and keyword arguments;
      keyword order does not matter).
    - A cached result older than `seconds` (according to `clock()`) is
      recomputed on the next call.
    - Exceptions are not cached: a call that raised is retried next time.
    - The decorated function gets a `cache_clear()` method that empties the
      cache, and a `cache_size()` method returning how many entries it holds.
    - `functools.wraps` is applied, so the name and docstring survive.
    """
    raise NotImplementedError
