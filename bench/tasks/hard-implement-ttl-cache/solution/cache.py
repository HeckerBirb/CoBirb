import functools
import time


def ttl_cache(seconds, clock=time.monotonic):
    def decorate(fn):
        store = {}

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            key = (args, tuple(sorted(kwargs.items())))
            hit = store.get(key)
            if hit is not None and clock() - hit[0] <= seconds:
                return hit[1]
            value = fn(*args, **kwargs)
            store[key] = (clock(), value)
            return value

        wrapper.cache_clear = store.clear
        wrapper.cache_size = lambda: len(store)
        return wrapper

    return decorate
