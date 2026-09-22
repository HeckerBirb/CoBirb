from parsing import parse_duration, parse_list, parse_pair


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
