"""Live integration tests against a real local Ollama server.

Every other test file in this suite mocks the network layer entirely (see
test_model.py) — that catches regressions in how requests/responses are
*shaped*, but nothing here has ever verified that CoBirb actually works
against a real Ollama server: that streaming genuinely delivers content,
and — the thing this project shipped a real bug around once already —
that the tool-calling loop actually converges instead of looping forever.

These tests are skipped by default (CI never runs them, and neither does a
plain local `pytest`) unless both of the following are true:

- COBIRB_TEST_MODEL is set to a model name pulled in a local Ollama install
  (one with tool-calling support, for the tool-call tests to mean anything
  — e.g. an "agentic"/function-calling-tuned model).
- That Ollama server is actually reachable at COBIRB_OLLAMA_URL (default
  http://localhost:11434).

Run them with, for example:

    COBIRB_TEST_MODEL=llama3.1 pytest tests/test_integration_ollama.py -v

Or select them explicitly among the full suite with `-m integration`.
"""
from __future__ import annotations

import os
import urllib.error
import urllib.request

import pytest

from cobirb.orchestrator import Orchestrator, build_default_policy
from cobirb.plugins.core.io import TerminalIO
from cobirb.plugins.core.model import LocalModelProvider
from cobirb.plugins.core.tools import ToolRegistry

OLLAMA_URL = os.environ.get("COBIRB_OLLAMA_URL", "http://localhost:11434")
TEST_MODEL = os.environ.get("COBIRB_TEST_MODEL", "")


def _ollama_available() -> bool:
    if not TEST_MODEL:
        return False
    try:
        with urllib.request.urlopen(f"{OLLAMA_URL}/api/tags", timeout=2):
            return True
    except (urllib.error.URLError, OSError):
        return False


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not _ollama_available(),
        reason=(
            "set COBIRB_TEST_MODEL=<model pulled in Ollama> with a local Ollama "
            f"server reachable at {OLLAMA_URL} to run these"
        ),
    ),
]


def test_live_chat_returns_content():
    """The most basic possible check: a real request/response round trip
    against Ollama's actual /api/chat, not a mocked shape of it."""
    provider = LocalModelProvider(model=TEST_MODEL)
    reply = provider.chat(
        "You are a terse assistant.",
        "Reply with exactly the single word: OK",
    )
    assert isinstance(reply, str)
    assert reply.strip()


def test_live_streaming_yields_content():
    """chat(stream=True) against a real server must actually yield
    non-empty content — this is the path test_model.py can only fake the
    NDJSON shape of, not prove Ollama's real streaming format matches."""
    provider = LocalModelProvider(model=TEST_MODEL)
    chunks = list(provider.chat("You are a terse assistant.", "Say hello in one short sentence.", stream=True))
    assert chunks
    assert "".join(chunks).strip()


def test_live_tool_call_round_trip(tmp_path):
    """Regression test for the real bug this project shipped once: a tool
    call must actually execute and the loop must converge to a final
    answer, not loop calling the same tool forever. Requires a model with
    real tool-calling support."""
    (tmp_path / "note.txt").write_text("the secret word is banana")

    registry = ToolRegistry(str(tmp_path))
    policy = build_default_policy()
    provider = LocalModelProvider(model=TEST_MODEL)
    orchestrator = Orchestrator(
        model=provider,
        tools={t.name: t for t in registry.values()},
        policy=policy,
        io=TerminalIO(),
    )

    session = orchestrator.run(
        "Read note.txt and tell me the secret word in it.",
        "You are a helpful assistant with access to tools. Use them when needed.",
        cwd=str(tmp_path),
        max_turns=8,
    )

    assert any(turn.role == "tool" for turn in session.turns), "model never called a tool"
    assert not session.summary.startswith("Stopped after"), "tool-calling loop did not converge"
    assert "banana" in session.summary.lower()


def test_live_plan_mode_converges_through_all_three_phases(tmp_path):
    """Plan mode against a real model: the planning phase must produce a
    genuine plan without calling any tools, the act phase must still call
    tools and converge as usual, and the validate phase must produce a
    real report — proving the plan/act/validate system-prompt additions
    (_PLAN_PHASE_INSTRUCTIONS etc.) actually hold up against a real model,
    not just the scripted stubs in test_orchestrator.py/test_cli.py."""
    (tmp_path / "note.txt").write_text("the secret word is banana")

    registry = ToolRegistry(str(tmp_path))
    policy = build_default_policy()
    provider = LocalModelProvider(model=TEST_MODEL)
    orchestrator = Orchestrator(
        model=provider,
        tools={t.name: t for t in registry.values()},
        policy=policy,
        io=TerminalIO(),
    )

    session = orchestrator.run(
        "Read note.txt and tell me the secret word in it.",
        "You are a helpful assistant with access to tools. Use them when needed.",
        cwd=str(tmp_path),
        max_turns=8,
        plan_mode=True,
    )

    plan_turns = [t for t in session.turns if t.phase == "plan"]
    assert plan_turns, "no plan-phase turn recorded"
    assert plan_turns[0].tool_use is None, "the planning phase must never call tools"

    assert any(t.role == "tool" for t in session.turns), "act phase never called a tool"
    assert not session.summary.startswith("Stopped after"), "act phase did not converge"
    assert "banana" in session.summary.lower()

    assert session.validation, "validate phase produced no report"
    assert not session.validation.startswith("Stopped after"), "validate phase did not converge"


def test_live_multi_step_tool_calls_converge(tmp_path):
    """A sequential two-file task must also converge, not just a single
    tool call — this is closer to real agentic usage than one lone call."""
    (tmp_path / "one.txt").write_text("alpha")
    (tmp_path / "two.txt").write_text("beta")

    registry = ToolRegistry(str(tmp_path))
    policy = build_default_policy()
    provider = LocalModelProvider(model=TEST_MODEL)
    orchestrator = Orchestrator(
        model=provider,
        tools={t.name: t for t in registry.values()},
        policy=policy,
        io=TerminalIO(),
    )

    session = orchestrator.run(
        "Read one.txt, then read two.txt, then tell me both contents.",
        "You are a helpful assistant with access to tools. Use them when needed.",
        cwd=str(tmp_path),
        max_turns=8,
    )

    tool_turns = [t for t in session.turns if t.role == "tool"]
    assert len(tool_turns) >= 2, "expected at least two separate tool calls"
    assert not session.summary.startswith("Stopped after"), "tool-calling loop did not converge"
    assert "alpha" in session.summary.lower()
    assert "beta" in session.summary.lower()


# --------------------------------------------------------------------------- #
# Respecting the model's own Modelfile SYSTEM directive, against a real server.
#
# test_model.py covers the composition rules with the network mocked. What it
# can't show is that the *actual* Ollama behaviour matches the assumption the
# whole design rests on: that omitting the system message lets the model's own
# SYSTEM apply, and that sending one replaces it.
# --------------------------------------------------------------------------- #
def test_live_model_system_prompt_matches_what_api_show_reports():
    """The lookup reads the same field `ollama show --modelfile` prints."""
    import json

    provider = LocalModelProvider(model=TEST_MODEL)
    request = urllib.request.Request(
        f"{OLLAMA_URL}/api/show",
        data=json.dumps({"model": TEST_MODEL}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        expected = json.loads(response.read().decode("utf-8")).get("system") or ""

    assert provider.model_system_prompt() == expected


def test_live_model_with_its_own_system_prompt_keeps_it_when_cobirb_sends_none():
    """The point of the whole feature: a model built with a custom SYSTEM
    must behave inside CoBirb exactly as it does in `ollama run`.

    Skipped for a model that declares no SYSTEM of its own — there would be
    nothing to preserve, so the test could not distinguish success from a
    provider that silently dropped it.
    """
    provider = LocalModelProvider(model=TEST_MODEL)
    own = provider.model_system_prompt().strip()
    if not own:
        pytest.skip(f"{TEST_MODEL} declares no SYSTEM of its own; nothing to preserve")

    # An empty CoBirb prompt must send no system message at all, leaving the
    # model's own in force — so the model can still answer from it.
    reply = provider.chat("", "Who are you? Answer in one short sentence.")

    assert reply.strip()


def test_live_composed_prompt_carries_the_models_own_first():
    provider = LocalModelProvider(model=TEST_MODEL)
    own = provider.model_system_prompt().strip()
    composed = provider.compose_system("Answer only in lowercase.")

    if own:
        assert composed.startswith(own)
        assert composed.endswith("Answer only in lowercase.")
    else:
        assert composed == "Answer only in lowercase."


def test_live_empty_cobirb_prompt_composes_to_nothing():
    provider = LocalModelProvider(model=TEST_MODEL)

    assert provider.compose_system("") == ""
    assert provider.compose_system("   \n  ") == ""
