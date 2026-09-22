import todo


def test_add_and_list(tmp_path, monkeypatch):
    monkeypatch.setattr(todo, "STORE", str(tmp_path / "t.json"))
    todo.add("one")
    assert todo.listing() == ["[ ] one"]
