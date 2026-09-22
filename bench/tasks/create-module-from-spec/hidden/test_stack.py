import pytest
from stack import Stack


def test_lifo():
    s = Stack(); s.push(1); s.push(2)
    assert s.pop() == 2 and s.pop() == 1


def test_peek_keeps_item():
    s = Stack(); s.push("a")
    assert s.peek() == "a" and len(s) == 1


def test_empty_errors():
    with pytest.raises(IndexError):
        Stack().pop()
    with pytest.raises(IndexError):
        Stack().peek()


def test_len():
    s = Stack()
    for i in range(4):
        s.push(i)
    assert len(s) == 4
