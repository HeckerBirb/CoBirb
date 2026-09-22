BASE_PORT = 8000
OFFSET = 81


def defaults():
    return {"host": "127.0.0.1", "port": BASE_PORT + OFFSET, "workers": 2}
