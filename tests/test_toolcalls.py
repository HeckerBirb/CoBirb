"""Tests for reading tool calls a model wrote as text."""
from __future__ import annotations

import pytest

from cobirb.plugins.core import toolcalls

SCHEMAS = {
    "read_file": {"type": "object", "properties": {"path": {"type": "string"},
                                                   "offset": {"type": "integer"}}},
    "write_file": {"type": "object", "properties": {"path": {"type": "string"},
                                                    "content": {"type": "string"}}},
}


def _calls(text):
    calls, problem = toolcalls.extract(text, SCHEMAS)
    return [(c.name, c.arguments) for c in calls], problem


def test_hermes_style_json_in_tool_call_tags():
    calls, problem = _calls('<tool_call>\n{"name": "read_file", "arguments": {"path": "a.py"}}\n</tool_call>')
    assert calls == [("read_file", {"path": "a.py"})] and not problem


def test_qwen_xml_keeps_file_contents_verbatim_and_types_numbers():
    text = (
        "<tool_call>\n<function=write_file>\n<parameter=path>\nx.py\n</parameter>\n"
        "<parameter=content>\ndef f():\n    return '<b>'\n</parameter>\n</function>\n</tool_call>"
    )
    calls, _ = _calls(text)
    assert calls == [("write_file", {"path": "x.py", "content": "def f():\n    return '<b>'"})]

    calls, _ = _calls("<function=read_file><parameter=path>a</parameter><parameter=offset>40</parameter></function>")
    assert calls == [("read_file", {"path": "a", "offset": 40})]


def test_qwen_xml_missing_closing_tags_is_still_read():
    """The strict parse of a missing close tag is what drops long writes upstream."""
    calls, _ = _calls("<tool_call>\n<function=read_file>\n<parameter=path>\nb.py\n</function>")
    assert calls == [("read_file", {"path": "b.py"})]


def test_several_calls_in_one_reply():
    text = ('<tool_call>{"name": "read_file", "arguments": {"path": "a"}}</tool_call>'
            '<tool_call>{"name": "read_file", "arguments": {"path": "b"}}</tool_call>')
    assert [a["path"] for _, a in _calls(text)[0]] == ["a", "b"]


def test_leaked_harmony_channel_markup():
    text = '<|channel|>commentary to=functions.read_file <|constrain|>json<|message|>{"path": "c.py"}<|call|>'
    assert _calls(text)[0] == [("read_file", {"path": "c.py"})]


@pytest.mark.parametrize("text", [
    '{"name": "read_file", "arguments": {"path": "d.py"}}',
    '```json\n{"name": "read_file", "parameters": {"path": "d.py"}}\n```',
    '{"function": {"name": "read_file", "arguments": "{\\"path\\": \\"d.py\\"}"}}',
])
def test_a_reply_that_is_only_a_json_call(text):
    assert _calls(text)[0] == [("read_file", {"path": "d.py"})]


def test_json_inside_an_explanation_is_not_a_call():
    text = ("Here is how you would configure it — the request body looks like this:\n\n"
            '```json\n{"name": "read_file", "arguments": {"path": "x"}}\n```\n\n'
            "and the server answers with the file's contents. " * 3)
    assert _calls(text) == ([], "")


def test_an_ordinary_answer_is_left_alone():
    assert _calls("The function returns 8081.") == ([], "")
    assert _calls('{"port": 8081, "host": "localhost"}') == ([], "")


def test_an_unknown_tool_is_reported_not_run():
    calls, problem = _calls('<tool_call>{"name": "reed_file", "arguments": {}}</tool_call>')
    assert calls == [] and "reed_file" in problem


def test_broken_json_in_call_tags_is_reported():
    calls, problem = _calls("<tool_call>{name: read_file, oops</tool_call>")
    assert calls == [] and "could not be read" in problem


def test_markup_is_stripped_for_replay():
    assert toolcalls.strip_markup('Let me look.<tool_call>{"name":"x"}</tool_call>') == "Let me look."


def test_a_server_parse_failure_is_recognised_only_at_the_start_of_a_reply():
    assert toolcalls.reply_is_server_parse_error("error parsing tool call: raw='<function=x>'")
    assert not toolcalls.reply_is_server_parse_error(
        "I fixed the bug where CoBirb reported 'error parsing tool call' as an answer."
    )
