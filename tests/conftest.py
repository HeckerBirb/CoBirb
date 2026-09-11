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
