import argparse


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("path")
    args = parser.parse_args(argv)
    with open(args.path) as handle:
        print(len(handle.read().splitlines()))


if __name__ == "__main__":
    main()
