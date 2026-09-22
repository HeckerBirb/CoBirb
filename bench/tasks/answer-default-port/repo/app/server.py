from .config import defaults


def serve(overrides=None):
    settings = {**defaults(), **(overrides or {})}
    return f"listening on {settings['host']}:{settings['port']}"
