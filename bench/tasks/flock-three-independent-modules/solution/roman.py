_PAIRS = [(1000, "M"), (900, "CM"), (500, "D"), (400, "CD"), (100, "C"), (90, "XC"),
          (50, "L"), (40, "XL"), (10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I")]


def to_roman(number):
    if not isinstance(number, int) or not 1 <= number <= 3999:
        raise ValueError(number)
    out = []
    for value, symbol in _PAIRS:
        while number >= value:
            out.append(symbol)
            number -= value
    return "".join(out)


def from_roman(text):
    for number in range(1, 4000):
        if to_roman(number) == text:
            return number
    raise ValueError(text)
