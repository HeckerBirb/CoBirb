import pytest
from users import create_user


def test_valid():
    assert create_user("  Ann ", 30) == {"name": "Ann", "age": 30}


def test_zero_age_ok():
    assert create_user("Bo", 0)["age"] == 0


@pytest.mark.parametrize("name", ["", "   "])
def test_empty_name(name):
    with pytest.raises(ValueError):
        create_user(name, 3)


def test_negative_age():
    with pytest.raises(ValueError):
        create_user("Cy", -1)
