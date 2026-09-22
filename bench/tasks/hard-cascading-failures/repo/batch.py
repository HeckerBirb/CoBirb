from config import settings
from ranges import chunks


def plan(items, size, **overrides):
    conf = settings(overrides)
    if size <= 0:
        raise TypeError("size must be positive")
    return {"batches": chunks(items, size), "retries": conf["retries"]}
