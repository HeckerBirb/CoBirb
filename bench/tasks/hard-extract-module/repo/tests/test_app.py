import pytest

import app
import jobs


def test_main():
    assert app.main(["timeout=2m", "tags=a, b"]) == "timeout=2m tags=a,b"


def test_schedule():
    assert jobs.schedule("1h;x,y") == {"every": 3600, "labels": ["x", "y"]}


def test_bad_duration():
    with pytest.raises(ValueError):
        jobs.schedule("soon;x")
