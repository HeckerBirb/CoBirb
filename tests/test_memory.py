"""Tests for memory catalogues: plaintext and encrypted round trips."""
from __future__ import annotations

import os
import stat

import pytest

from cobirb import memory
from cobirb.plugins.core.crypto import AesGcmScryptSessionCrypto


@pytest.fixture
def crypto():
    return AesGcmScryptSessionCrypto()


def test_create_plain_catalogue_with_no_password(tmp_path, crypto):
    cat = memory.create(str(tmp_path), "work", crypto, None)
    assert cat.encrypted is False
    assert cat.path.endswith("work.md")
    assert os.path.isfile(cat.path)


def test_create_plain_catalogue_with_blank_password(tmp_path, crypto):
    cat = memory.create(str(tmp_path), "work", crypto, "")
    assert cat.encrypted is False


def test_create_encrypted_catalogue_with_password(tmp_path, crypto):
    cat = memory.create(str(tmp_path), "personal", crypto, "hunter2")
    assert cat.encrypted is True
    assert cat.path.endswith("personal.md.enc")


def test_create_refuses_duplicate_name_across_either_extension(tmp_path, crypto):
    memory.create(str(tmp_path), "work", crypto, None)
    with pytest.raises(memory.CatalogueError):
        memory.create(str(tmp_path), "work", crypto, "pw")


def test_create_refuses_blank_name(tmp_path, crypto):
    with pytest.raises(memory.CatalogueError):
        memory.create(str(tmp_path), "  ", crypto, None)


def test_plain_round_trip(tmp_path, crypto):
    cat = memory.create(str(tmp_path), "work", crypto, None)
    memory.append_fact(cat, "The user prefers tabs over spaces.")
    memory.save(cat, crypto)

    reloaded = memory.load(cat.path, crypto)
    assert reloaded.facts == ["The user prefers tabs over spaces."]
    assert reloaded.encrypted is False


def test_encrypted_round_trip(tmp_path, crypto):
    cat = memory.create(str(tmp_path), "personal", crypto, "hunter2")
    memory.append_fact(cat, "Call the user Bob.")
    memory.save(cat, crypto)

    reloaded = memory.load(cat.path, crypto, "hunter2")
    assert reloaded.facts == ["Call the user Bob."]


def test_wrong_password_raises_catalogue_error(tmp_path, crypto):
    cat = memory.create(str(tmp_path), "personal", crypto, "hunter2")
    memory.save(cat, crypto)
    with pytest.raises(memory.CatalogueError):
        memory.load(cat.path, crypto, "wrong")


def test_save_reencrypts_rather_than_reusing_ciphertext(tmp_path, crypto):
    """Every save must produce different bytes (fresh salt/nonce), never an
    in-place append to the previous blob."""
    cat = memory.create(str(tmp_path), "personal", crypto, "hunter2")
    memory.append_fact(cat, "one")
    memory.save(cat, crypto)
    with open(cat.path, "rb") as fh:
        first = fh.read()

    memory.append_fact(cat, "two")
    memory.save(cat, crypto)
    with open(cat.path, "rb") as fh:
        second = fh.read()

    assert first != second


def test_discover_catalogues_lists_both_kinds(tmp_path, crypto):
    memory.create(str(tmp_path), "work", crypto, None)
    memory.create(str(tmp_path), "personal", crypto, "hunter2")

    rows = memory.discover_catalogues(str(tmp_path))
    names = {(row.name, row.encrypted) for row in rows}
    assert names == {("work", False), ("personal", True)}


def test_discover_catalogues_returns_empty_list_for_missing_directory(tmp_path):
    assert memory.discover_catalogues(str(tmp_path / "nope")) == []


def test_ensure_public_exists_creates_an_empty_catalogue(tmp_path, monkeypatch):
    monkeypatch.setenv("COBIRB_HOME", str(tmp_path))
    path = memory.ensure_public_exists()
    assert os.path.isfile(path)
    assert path.endswith(os.path.join("memories", "public.md"))


def test_ensure_public_exists_is_idempotent(tmp_path, monkeypatch, crypto):
    monkeypatch.setenv("COBIRB_HOME", str(tmp_path))
    path = memory.ensure_public_exists()
    cat = memory.load(path, crypto)
    memory.append_fact(cat, "already here")
    memory.save(cat, crypto)

    memory.ensure_public_exists()  # must not overwrite what's already there
    reloaded = memory.load(path, crypto)
    assert reloaded.facts == ["already here"]


def test_delete_removes_the_file(tmp_path, crypto):
    cat = memory.create(str(tmp_path), "work", crypto, None)
    memory.delete(cat.path)
    assert not os.path.isfile(cat.path)


def test_rename_keeps_encryption_suffix(tmp_path, crypto):
    cat = memory.create(str(tmp_path), "personal", crypto, "hunter2")
    new_path = memory.rename(cat.path, "renamed")
    assert new_path.endswith("renamed.md.enc")
    assert os.path.isfile(new_path)
    assert not os.path.isfile(cat.path)


def test_rename_refuses_existing_name(tmp_path, crypto):
    memory.create(str(tmp_path), "taken", crypto, None)
    cat = memory.create(str(tmp_path), "work", crypto, None)
    with pytest.raises(memory.CatalogueError):
        memory.rename(cat.path, "taken")


def test_render_is_empty_for_no_facts():
    cat = memory.MemoryCatalogue(name="work", path="/tmp/work.md", encrypted=False, facts=[])
    assert cat.render() == ""


def test_render_names_the_catalogue_and_lists_facts():
    cat = memory.MemoryCatalogue(
        name="work", path="/tmp/work.md", encrypted=False, facts=["fact one", "fact two"]
    )
    rendered = cat.render()
    assert "work" in rendered
    assert "fact one" in rendered
    assert "fact two" in rendered


def test_a_multi_line_fact_survives_a_round_trip(tmp_path, crypto):
    """The prompt box is a multi-line editor, so /remember accepts newlines.
    Written flat, everything after the first line was silently dropped on
    reload — the file looked fine and half the memory was gone."""
    cat = memory.create(str(tmp_path), "work", crypto, None)
    memory.append_fact(cat, "line one\nline two\nline three")
    memory.save(cat, crypto)

    assert memory.load(cat.path, crypto).facts == ["line one\nline two\nline three"]


def test_a_multi_line_fact_survives_in_an_encrypted_catalogue(tmp_path, crypto):
    cat = memory.create(str(tmp_path), "secret", crypto, "pw")
    memory.append_fact(cat, "first\nsecond")
    memory.save(cat, crypto)

    assert memory.load(cat.path, crypto, "pw").facts == ["first\nsecond"]


def test_several_facts_including_a_multi_line_one_stay_separate(tmp_path, crypto):
    cat = memory.create(str(tmp_path), "work", crypto, None)
    memory.append_fact(cat, "plain one")
    memory.append_fact(cat, "multi\nline")
    memory.append_fact(cat, "plain two")
    memory.save(cat, crypto)

    assert memory.load(cat.path, crypto).facts == ["plain one", "multi\nline", "plain two"]


def test_a_catalogue_is_never_world_readable_even_for_an_instant(tmp_path, crypto, monkeypatch):
    """open()+chmod leaves the file at the umask's mode until the chmod
    lands — 0666 on a permissive umask, i.e. world *writable*. A public
    catalogue is plaintext and is read straight into the system prompt, so
    that window is one in which another local account can put words in the
    model's mouth.

    Run under a genuinely permissive umask, because that is the only setting
    under which the old code was wrong.
    """
    seen = []
    real_chmod = os.chmod

    def watching_chmod(path, mode, *args, **kwargs):
        seen.append(stat.S_IMODE(os.stat(path).st_mode))
        return real_chmod(path, mode, *args, **kwargs)

    monkeypatch.setattr(os, "chmod", watching_chmod)
    previous = os.umask(0o000)
    try:
        cat = memory.create(str(tmp_path), "public", crypto, None)
        memory.save(cat, crypto)
    finally:
        os.umask(previous)

    assert seen == []  # no chmod-after-create window to observe in the first place
    assert stat.S_IMODE(os.stat(cat.path).st_mode) == 0o600


def test_an_encrypted_catalogue_is_created_private_too(tmp_path, crypto):
    cat = memory.create(str(tmp_path), "secret", crypto, "pw")
    assert stat.S_IMODE(os.stat(cat.path).st_mode) == 0o600
