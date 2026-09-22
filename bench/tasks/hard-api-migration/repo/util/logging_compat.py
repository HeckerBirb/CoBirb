import inspect

from .logger import get_logger


def log(message, level="info"):
    """Deprecated: use util.logger.get_logger(__name__).<level>(message)."""
    caller = inspect.stack()[1].frame.f_globals.get("__name__", "?")
    getattr(get_logger(caller), level)(message)
