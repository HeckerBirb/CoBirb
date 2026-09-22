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


def format_duration(seconds):
    for unit, size in (("h", 3600), ("m", 60)):
        if seconds % size == 0 and seconds >= size:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"


def main(argv):
    settings = dict(parse_pair(arg) for arg in argv)
    timeout = parse_duration(settings.get("timeout", "30s"))
    tags = parse_list(settings.get("tags", ""))
    return f"timeout={format_duration(timeout)} tags={','.join(tags)}"
