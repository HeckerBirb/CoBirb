import pytest
from duration import parse_duration


def test_units():
    assert parse_duration("2h") == 7200
    assert parse_duration("3m") == 180
    assert parse_duration("45s") == 45


def test_combined():
    assert parse_duration("1h30m") == 5400


@pytest.mark.parametrize("bad", ["", "abc", "10x", "5"])
def test_invalid(bad):
    with pytest.raises(ValueError):
        parse_duration(bad)
