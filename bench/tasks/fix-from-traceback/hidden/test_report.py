from report import summarise


def test_skips_missing_email():
    assert summarise([{"email": "A@x"}, {"name": "b"}, {"email": "c@X"}]) == "a@x, c@x"


def test_all_present():
    assert summarise([{"email": "Q@q"}]) == "q@q"
