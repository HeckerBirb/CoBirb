def run(store, line):
    parts = line.split()
    command, args = parts[0], parts[1:]
    if command == "set":
        store.set(args[0], " ".join(args[1:]))
        return "OK"
    if command == "get":
        value = store.get(args[0])
        return "(nil)" if value is None else value
    if command == "del":
        return "1" if store.delete(args[0]) else "0"
    if command == "keys":
        return " ".join(store.keys())
    return "ERR"
