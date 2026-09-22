import todo


def _use(tmp_path, monkeypatch):
    monkeypatch.setattr(todo, "STORE", str(tmp_path / "t.json"))


def test_cli_priority_orders_the_list(tmp_path, monkeypatch, capsys):
    _use(tmp_path, monkeypatch)
    todo.main(["add", "later"])
    todo.main(["add", "now", "--priority", "1"])
    todo.main(["add", "soon", "--priority", "2"])
    todo.main(["add", "also later"])
    capsys.readouterr()
    todo.main(["list"])
    lines = capsys.readouterr().out.strip().splitlines()
    assert [line.split("] ", 1)[1].split(" (")[0].strip() for line in lines] == ["now", "soon", "later", "also later"]


def test_default_priority_is_three(tmp_path, monkeypatch):
    _use(tmp_path, monkeypatch)
    todo.main(["add", "x"])
    assert todo.load()[0]["priority"] == 3
