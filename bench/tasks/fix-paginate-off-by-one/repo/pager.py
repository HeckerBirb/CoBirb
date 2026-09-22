def paginate(items, page, size):
    """Return the given 1-based page of ``items``, ``size`` items per page."""
    start = page * size
    return items[start:start + size]
