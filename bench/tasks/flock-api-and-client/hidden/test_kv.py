from cli import run
from store import Store


def test_store():
    s = Store(); s.set("b", "2"); s.set("a", "1")
    assert s.get("a") == "1" and s.get("zz", "d") == "d"
    assert s.keys() == ["a", "b"]
    assert s.delete("a") is True and s.delete("a") is False


def test_cli():
    s = Store()
    assert run(s, "set k v") == "OK"
    assert run(s, "get k") == "v"
    assert run(s, "get nope") == "(nil)"
    assert run(s, "set j w") == "OK"
    assert run(s, "keys") == "j k"
    assert run(s, "del k") == "1" and run(s, "del k") == "0"
