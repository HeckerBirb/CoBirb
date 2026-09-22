from geometry import rectangle_area, square_area


def test_rectangle():
    assert rectangle_area(3, 4) == 12


def test_square():
    assert square_area(5) == 25
