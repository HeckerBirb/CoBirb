import re

_DURATION = re.compile(r"^(\d+)([smh])$")


def parse_duration(text):
    match = _DURATION.match(text.strip())
    if not match:
        raise ValueError(text)
    value, unit = int(match.group(1)), match.group(2)
    return value * {"s": 1, "m": 60, "h": 3600}[unit]


def parse_pair(text, sep="="):
    key, found, value = text.partition(sep)
    if not found:
        raise ValueError(text)
    return key.strip(), value.strip()


def parse_list(text):
    return [part.strip() for part in text.split(",") if part.strip()]


