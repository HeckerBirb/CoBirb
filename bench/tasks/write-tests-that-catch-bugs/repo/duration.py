import re

_PART = re.compile(r"(\d+)([hms])")
_UNIT = {"h": 3600, "m": 60, "s": 1}


def parse_duration(text):
    """Seconds in a duration like "1h30m", "45s" or "2h". Raises ValueError otherwise."""
    text = text.strip()
    if not text or _PART.sub("", text):
        raise ValueError(f"not a duration: {text!r}")
    return sum(int(n) * _UNIT[u] for n, u in _PART.findall(text))
