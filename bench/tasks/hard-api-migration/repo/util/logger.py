RECORDS = []


class _Logger:
    def __init__(self, name):
        self.name = name

    def _emit(self, level, message):
        RECORDS.append((self.name, level, message))

    def debug(self, message):
        self._emit("debug", message)

    def info(self, message):
        self._emit("info", message)

    def warning(self, message):
        self._emit("warning", message)

    def error(self, message):
        self._emit("error", message)


def get_logger(name):
    return _Logger(name)
