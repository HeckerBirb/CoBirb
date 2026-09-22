import argparse
import json
import os

STORE = os.environ.get("TODO_FILE", "todo.json")


def load():
    if not os.path.exists(STORE):
        return []
    with open(STORE) as fh:
        return json.load(fh)


def save(items):
    with open(STORE, "w") as fh:
        json.dump(items, fh)


def add(text):
    items = load()
    items.append({"text": text, "done": False})
    save(items)


def listing():
    return [f"[{'x' if item['done'] else ' '}] {item['text']}" for item in load()]


def main(argv=None):
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    adding = sub.add_parser("add")
    adding.add_argument("text")
    sub.add_parser("list")
    args = parser.parse_args(argv)
    if args.command == "add":
        add(args.text)
    else:
        print("\n".join(listing()))


if __name__ == "__main__":
    main()
