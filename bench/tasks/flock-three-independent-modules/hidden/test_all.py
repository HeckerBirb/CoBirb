import pytest
from roman import from_roman, to_roman
from units import to_celsius, to_fahrenheit
from wordfreq import top_words


def test_units():
    assert to_celsius(212) == 100.0 and to_fahrenheit(37) == 98.6 and to_celsius(0) == -17.8


@pytest.mark.parametrize("n,r", [(1, "I"), (4, "IV"), (9, "IX"), (1994, "MCMXCIV"), (3999, "MMMCMXCIX")])
def test_roman_round_trip(n, r):
    assert to_roman(n) == r and from_roman(r) == n


def test_roman_bounds():
    with pytest.raises(ValueError):
        to_roman(0)
    with pytest.raises(ValueError):
        from_roman("IIII X")


def test_top_words():
    text = "The cat and the hat. The cat's hat! And AND"
    assert top_words(text, 3) == [("and", 3), ("the", 3), ("hat", 2)]
