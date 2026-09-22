from text import slugify


def test_basic():
    assert slugify("Hello, World!") == "hello-world"


def test_runs_collapse():
    assert slugify("a  --  b") == "a-b"


def test_edges_stripped():
    assert slugify("  --Rock & Roll--  ") == "rock-roll"


def test_digits_kept():
    assert slugify("Top 10 Tips") == "top-10-tips"


def test_empty():
    assert slugify("!!!") == ""
