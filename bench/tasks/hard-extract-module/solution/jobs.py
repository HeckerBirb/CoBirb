from parsing import parse_duration, parse_list


def schedule(spec):
    every, _, labels = spec.partition(";")
    return {"every": parse_duration(every), "labels": parse_list(labels)}
