import pytest

from batch import plan
from config import settings
from ranges import chunks


def test_chunks_keep_the_last_item():
    assert chunks([1, 2, 3], 2) == [[1, 2], [3]]


def test_overrides_do_not_leak_between_calls():
    settings({"retries": 9})
    assert settings()["retries"] == 3


def test_plan():
    assert plan([1, 2, 3, 4, 5], 2, retries=1) == {"batches": [[1, 2], [3, 4], [5]], "retries": 1}
    assert plan([1], 1)["retries"] == 3


def test_bad_size_is_a_value_error():
    with pytest.raises(ValueError):
        plan([1], 0)
