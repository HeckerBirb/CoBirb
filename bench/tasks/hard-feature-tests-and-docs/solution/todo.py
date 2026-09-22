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


def add(text, priority=3):
    items = load()
    items.append({"text": text, "done": False, "priority": priority})
    save(items)


def listing():
    ordered = sorted(load(), key=lambda item: item.get("priority", 3))
    return [f"[{'x' if item['done'] else ' '}] {item['text']}" for item in ordered]


def main(argv=None):
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    adding = sub.add_parser("add")
    adding.add_argument("text")
    adding.add_argument("--priority", type=int, default=3)
    sub.add_parser("list")
    args = parser.parse_args(argv)
    if args.command == "add":
        add(args.text, args.priority)
    else:
        print("\n".join(listing()))


if __name__ == "__main__":
    main()
