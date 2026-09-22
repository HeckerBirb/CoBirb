import argparse


def main(argv=None):
    parser = argparse.ArgumentParser(description="Fetch records from the archive.")
    parser.add_argument("query")
    parser.add_argument("--limit", type=int, default=20, help="maximum number of records (default 20)")
    parser.add_argument("--json", action="store_true", help="print JSON instead of text")
    return parser.parse_args(argv)
