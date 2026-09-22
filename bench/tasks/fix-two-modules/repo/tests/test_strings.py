from strings import initials


def test_initials_are_upper_case():
    assert initials("grace brewster hopper") == "GBH"
