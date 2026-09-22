import time

from .errors import BadSignature


def sign(payload, key):
    return f"{payload}.{hash((payload, key))}"


def verify(token, key):
    payload, _, signature = token.rpartition(".")
    if str(hash((payload, key))) != signature:
        raise BadSignature(token)
    return payload
