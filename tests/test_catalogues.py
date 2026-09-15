"""Tests for CatalogueStore — the catalogues one session has open.

The happy paths are covered end-to-end through the TUI (test_tui.py); what
is here is the error contract, which is the part a screen can't easily
provoke: every fallible method answers with a sentence to show the user
rather than raising, and "" means it worked.
"""
from __future__ import annotations

import pytest

from cobirb.runtime.catalogues import CatalogueStore


@pytest.fixture
def store(tmp_path):
    return CatalogueStore(str(tmp_path))


def test_a_new_store_has_nothing_loaded(store):
    """Existing on disk and feeding the model are separate things — a store
    never auto-loads."""
    store.ensure_public()
    assert [row.name for row in store.rows()] == ["public"]
    assert store.loaded == {}


def test_creating_a_catalogue_loads_it(store):
    assert store.create("work", "") == ""
    assert "work" in store.loaded


def test_creating_a_duplicate_reports_rather_than_raises(store):
    store.create("work", "")
    assert "already exists" in store.create("work", "")


def test_creating_with_a_path_for_a_name_reports_rather_than_raises(store):
    assert "not a usable name" in store.create("../escaped", "")
    assert store.loaded == {}


def test_loading_with_the_wrong_password_reports_and_loads_nothing(store):
    store.create("secret", "hunter2")
    store.unload("secret")
    row = next(r for r in store.rows() if r.name == "secret")

    assert store.load(row, "wrong") != ""
    assert "secret" not in store.loaded


def test_remembering_into_a_catalogue_that_is_not_loaded_reports(store):
    store.create("work", "")
    store.unload("work")
    assert "not loaded" in store.remember("work", "a fact")


def test_deleting_something_that_is_not_there_reports(store):
    assert "No catalogue named" in store.delete("nope")


def test_renaming_something_that_is_not_there_reports(store):
    assert "No catalogue named" in store.rename("nope", "elsewhere")


def test_renaming_a_loaded_catalogue_keeps_it_loaded_under_the_new_name(store):
    store.create("work", "")
    assert store.rename("work", "renamed") == ""
    assert "work" not in store.loaded
    assert store.loaded["renamed"].name == "renamed"


def test_deleting_a_loaded_catalogue_unloads_it(store):
    store.create("work", "")
    assert store.delete("work") == ""
    assert store.loaded == {}


def test_the_system_prompt_is_empty_with_nothing_loaded(store):
    assert store.system_prompt() == ""


def test_the_system_prompt_names_each_loaded_catalogue_and_its_facts(store):
    store.create("work", "")
    store.remember("work", "keep commits short")
    store.create("personal", "")
    store.remember("personal", "call the user Bob")

    prompt = store.system_prompt()
    assert "work" in prompt and "keep commits short" in prompt
    assert "personal" in prompt and "call the user Bob" in prompt


def test_an_empty_loaded_catalogue_contributes_nothing_to_the_prompt(store):
    store.create("work", "")
    assert store.system_prompt() == ""


def test_the_crypto_backend_is_resolved_once_and_reused(store):
    assert store.crypto is store.crypto
