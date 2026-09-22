import todo


def test_priority(tmp_path, monkeypatch):
    monkeypatch.setattr(todo, "STORE", str(tmp_path / "t.json"))
    todo.add("b", 2)
    todo.add("a", 1)
    assert todo.listing() == ["[ ] a", "[ ] b"]
