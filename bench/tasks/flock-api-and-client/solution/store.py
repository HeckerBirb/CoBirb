class Store:
    def __init__(self):
        self._data = {}

    def set(self, key, value):
        self._data[key] = value

    def get(self, key, default=None):
        return self._data.get(key, default)

    def delete(self, key):
        return self._data.pop(key, None) is not None

    def keys(self):
        return sorted(self._data)
