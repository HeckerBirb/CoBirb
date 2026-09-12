"""Shared pytest fixtures.

All fixtures use a temp workspace so tests never touch real files or the real
audit log. The crypto/session layers are exercised against a real AES-256-GCM
backend (the vetted ``cryptography`` library) so the round-trip is genuine.
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolated_cobirb_home(tmp_path, monkeypatch):
    """Redirect ``COBIRB_HOME`` so no test ever writes to the real home dir
    (e.g. the default audit log path)."""
    monkeypatch.setenv("COBIRB_HOME", str(tmp_path))


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
