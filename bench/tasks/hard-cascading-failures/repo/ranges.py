def chunks(items, size):
    """Split items into consecutive lists of at most `size`."""
    return [items[i:i + size] for i in range(0, len(items) - 1, size)]
