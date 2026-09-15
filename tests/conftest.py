"""Shared pytest fixtures.

All fixtures use a temp workspace so tests never touch real files or the real
audit log. The crypto/session layers are exercised against a real AES-256-GCM
backend (the vetted ``cryptography`` library) so the round-trip is genuine.
"""
from __future__ import annotations

import json
import os

import pytest

from cobirb.plugins.core import crypto as crypto_module

# scrypt's interactive cost (RFC 7914), ~16 MiB — see `_fast_key_derivation`.
_TEST_SCRYPT_N = 2**14


@pytest.fixture(autouse=True)
def _fast_key_derivation(monkeypatch):
    """Derive session keys at scrypt's interactive cost for the suite.

    The shipped cost is deliberately expensive — ~128 MiB and about 200 ms per
    derivation — and several hundred tests here open or save an encrypted
    session or memory catalogue. Paying it every time spends minutes of every
    run demonstrating that scrypt is slow on purpose, which is not a claim any
    of those tests are making.

    Only the work factor moves. The cipher, the blob format, the header that
    records which parameters produced a blob, and the full encrypt/decrypt
    round trip are all still exercised for real. ``tests/test_crypto.py``
    restores the shipped cost, because there the KDF itself is the subject.
    """
    monkeypatch.setattr(crypto_module, "_SCRYPT_N", _TEST_SCRYPT_N)


@pytest.fixture(autouse=True)
def _isolated_cobirb_home(tmp_path, monkeypatch):
    """Redirect ``COBIRB_HOME`` so no test ever writes to the real home dir
    (e.g. the default audit log path)."""
    monkeypatch.setenv("COBIRB_HOME", str(tmp_path))


def write_config(home, data) -> str:
    """Write CoBirb's config file under ``home`` and return its path.

    There is one config file — ``<home>/.cobirb/config.json`` — and no repo
    layer, so tests that want a setting in force write it here. Worth a shared
    helper because the location is a *rule* now rather than one of two options
    (see ``cobirb.config``): a test that reached for a project-local file would
    be asserting on something CoBirb deliberately does not read, and would
    pass for the wrong reason if that ever changed.
    """
    directory = os.path.join(str(home), ".cobirb")
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, "config.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    return path


@pytest.fixture
def tmp_workspace(tmp_path):
    """A clean working directory for tests."""
    return tmp_path


# --------------------------------------------------------------------------- #
# Shared test doubles.
#
# Only the ones that were genuinely identical across files live here. The
# specialised doubles — test_orchestrator's _RecordingIO subclasses,
# test_tui's _StubOrchestrator variants — stay where they are: they are each
# shaped around one file's questions, and forcing them into a common
# ancestor would make every one of them carry the others' concerns.
# --------------------------------------------------------------------------- #
class DummyModel:
    """A model that returns a fixed reply and never calls tools."""

    def __init__(self, reply="hello"):
        self.reply = reply

    def chat(self, *args, **kwargs):
        return self.reply

    def parse_tool_calls(self, reply):
        return []

    def supports_tool_calling(self):
        return False

    def supports_streaming(self):
        return False


class StubSession:
    """A finished session, as far as a caller reading the result can tell."""

    summary = "ok"
    validation = ""
    turns = ()


class StubSessionManager:
    """Records the passwords it was asked to save under, and nothing else."""

    def __init__(self):
        self.saved_with = []
        self.session = StubSession()

    def save(self, password):
        self.saved_with.append(password)


@pytest.fixture
def dummy_model():
    return DummyModel()
