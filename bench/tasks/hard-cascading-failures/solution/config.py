DEFAULTS = {"retries": 3, "backoff": 0.5}


def settings(overrides=None):
    merged = dict(DEFAULTS)
    merged.update(overrides or {})
    return merged
