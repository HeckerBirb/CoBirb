import time

from .errors import TokenExpired
from .tokens import verify


def validate_session(token, key, issued_at, ttl=3600):
    payload = verify(token, key)
    if time.time() - issued_at > ttl:
        raise TokenExpired(payload)
    return payload


def refresh(session):
    return {**session, "issued_at": time.time()}
