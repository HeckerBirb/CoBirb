import argparse


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("path")
    parser.add_argument("--words", action="store_true")
    args = parser.parse_args(argv)
    with open(args.path) as handle:
        text = handle.read()
    print(len(text.split()) if args.words else len(text.splitlines()))


if __name__ == "__main__":
    main()
