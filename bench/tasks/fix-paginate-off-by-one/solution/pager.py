def paginate(items, page, size):
    start = (page - 1) * size
    return items[start:start + size]
