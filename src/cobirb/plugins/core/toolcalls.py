"""Tool calls a model wrote as text instead of making them.

CoBirb reads tool calls from the server's structured ``tool_calls`` field
first, and that remains the path a well-behaved model takes. But local models
routinely put the call in their *reply* instead: Qwen3-coder switches to its
XML format once it is offered more than a handful of tools, Hermes-style models
write ``<tool_call>`` JSON, gpt-oss leaks its channel markup when a template is
off, and small models print a bare JSON object. When nothing read those, the
loop took the call for the model's final answer and the task ended there — the
same failure that was once the commonest way a flock died, where the charter
sat in a reply nobody parsed.

Reading them is not a permission question: every call found here goes through
the same policy gate, approval prompt and audit as a native one. The care is
about not *inventing* calls:

- only a name CoBirb actually offered this turn counts;
- explicit call syntax (``<tool_call>``, ``<function=``, a channel marker) is
  read wherever it appears, because nobody writes that in passing;
- plain JSON counts only when it is essentially the whole reply, so a JSON
  example inside an explanation is never executed.

Something that clearly *is* a call but cannot be read — unknown tool, broken
JSON, a parser error the server returned as the answer — is reported back as
a problem for the model to correct, rather than run or silently dropped.
"""
from __future__ import annotations

import json
import re
from typing import Any

from ...typing.spi import ToolCall

_TOOL_CALL_BLOCK = re.compile(r"<tool_call>(.*?)(?:</tool_call>|\Z)", re.S)
_FUNCTION_BLOCK = re.compile(r"<function=([^>\s]+)>(.*?)(?:</function>|(?=<function=)|\Z)", re.S)
_PARAMETER = re.compile(
    r"<parameter=([^>\s]+)>\n?(.*?)\n?(?:</parameter>|(?=<parameter=)|(?=</function>)|\Z)", re.S
)
_HARMONY = re.compile(r"to=functions\.([\w.-]+)[^<]*(?:<\|constrain\|>\w+)?<\|message\|>(.*?)(?:<\|call\|>|\Z)", re.S)
_FENCE = re.compile(r"```(?:json|tool_call|tool)?\s*\n(.*?)```", re.S)
_MARKUP = re.compile(r"<tool_call>.*?(?:</tool_call>|\Z)|<function=.*?(?:</function>|\Z)", re.S)

# What a server's own tool-call parser failure looks like when it comes back
# as the reply or an error body (ollama/ollama#18563 returns it with HTTP 200).
_SERVER_PARSE_ERROR = re.compile(
    r"pars\w* tool[ _-]?call|tool[ _-]?call\W.{0,60}(?:pars|invalid|malformed)|XML syntax error",
    re.I | re.S,
)

# How much prose may surround a bare JSON call before it reads as an example.
_BARE_JSON_SLACK = 160


# The same failure arriving as the reply itself. Anchored to the start of the
# reply: a model *discussing* tool-call parsing (working on CoBirb, say) must
# not have its answer thrown away.
_SERVER_PARSE_ERROR_REPLY = re.compile(
    r"\s*(?:error|failed)\b[^\n]{0,40}pars\w* tool[ _-]?call|\s*XML syntax error", re.I
)


def looks_like_server_parse_error(text: str) -> bool:
    """Whether an error body says the server could not parse a tool call."""
    return bool(text) and bool(_SERVER_PARSE_ERROR.search(text))


def reply_is_server_parse_error(text: str) -> bool:
    """Whether a *reply* is really the server's tool-call parser failing."""
    return bool(text) and bool(_SERVER_PARSE_ERROR_REPLY.match(text)) and len(text) < 2000


def strip_markup(text: str) -> str:
    """``text`` without any tool-call markup, for replaying it as history."""
    return _MARKUP.sub("", text).strip()


def extract(text: str, schemas: dict[str, dict[str, Any]]) -> tuple[list[ToolCall], str]:
    """Tool calls written into ``text``, and a problem if one could not be read.

    ``schemas`` maps each offered tool name to its JSON-schema ``parameters``.
    Returns ``([], "")`` for an ordinary reply.
    """
    if not text or not schemas:
        return [], ""
    calls: list[ToolCall] = []
    problems: list[str] = []

    blocks = _TOOL_CALL_BLOCK.findall(text)
    xml_sources = [b for b in blocks if "<function=" in b] or (
        [text] if "<function=" in text and not blocks else []
    )
    for source in xml_sources:
        for name, body in _FUNCTION_BLOCK.findall(source):
            _accept(name, _xml_arguments(body, schemas.get(name, {})), schemas, calls, problems)
    for block in blocks:
        if "<function=" not in block:
            _from_json(block, schemas, calls, problems)
    for name, body in _HARMONY.findall(text):
        arguments = _loads(body)
        if isinstance(arguments, dict):
            _accept(name, arguments, schemas, calls, problems)
        else:
            problems.append(f"a {name} call whose arguments were not valid JSON")

    if not calls and not problems:
        # Plain JSON: only when the call *is* the reply, fenced or bare.
        fenced = _FENCE.findall(text)
        candidates = fenced if len(fenced) == 1 else []
        outside = _FENCE.sub("", text) if candidates else ""
        if not candidates and text.strip().startswith(("{", "[")):
            candidates, outside = [text.strip()], ""
        if candidates and len(outside.strip()) <= _BARE_JSON_SLACK:
            _from_json(candidates[0], schemas, calls, problems, quiet=True)

    return calls, "; ".join(dict.fromkeys(problems))


def _from_json(source: str, schemas: dict[str, dict[str, Any]], calls: list[ToolCall],
               problems: list[str], *, quiet: bool = False) -> None:
    value = _loads(source)
    if value is None:
        if not quiet:
            problems.append("a tool call whose JSON could not be read")
        return
    for item in value if isinstance(value, list) else [value]:
        name, arguments = _shape(item)
        if name is None:
            if not quiet:
                problems.append("a tool call without a tool name")
            continue
        if quiet and name not in schemas:
            continue  # plain JSON that merely mentions a name is not a call
        _accept(name, arguments, schemas, calls, problems)


def _shape(item: Any) -> tuple[str | None, Any]:
    """``(name, arguments)`` out of the JSON shapes models use for a call."""
    if not isinstance(item, dict):
        return None, None
    if isinstance(item.get("function"), dict):
        item = item["function"]
    name = item.get("name") or item.get("tool") or item.get("tool_name")
    arguments = item.get("arguments", item.get("parameters", item.get("args", item.get("input", {}))))
    return (str(name) if name else None), arguments


def _accept(name: str, arguments: Any, schemas: dict[str, dict[str, Any]],
            calls: list[ToolCall], problems: list[str]) -> None:
    if name not in schemas:
        problems.append(f"a call to '{name}', which is not one of the available tools")
        return
    if isinstance(arguments, str):
        arguments = _loads(arguments) if arguments.strip() else {}
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        problems.append(f"a {name} call whose arguments were not an object")
        return
    calls.append(ToolCall(name=name, arguments=arguments))


def _xml_arguments(body: str, schema: dict[str, Any]) -> dict[str, Any]:
    """Qwen3-coder's ``<parameter=key>value</parameter>`` pairs, typed by schema.

    Values are raw text. A parameter the schema says is not a string (a number,
    a flag, a list) is read as JSON when it parses as such; everything else —
    file contents above all — is kept exactly as written, which is why this is
    a tolerant regex and not an XML parser: Ollama's own strict parse is what
    rejects long file writes (ollama/ollama#18563).
    """
    properties = schema.get("properties", {}) if isinstance(schema, dict) else {}
    arguments: dict[str, Any] = {}
    for key, value in _PARAMETER.findall(body):
        kind = properties.get(key, {}).get("type")
        if kind in ("integer", "number", "boolean", "array", "object"):
            parsed = _loads(value)
            arguments[key] = parsed if parsed is not None else value
        else:
            arguments[key] = value
    return arguments


def _loads(source: str) -> Any:
    """Parse the first JSON value in ``source``, tolerating trailing junk."""
    source = source.strip()
    if not source:
        return None
    try:
        return json.loads(source)
    except json.JSONDecodeError:
        pass
    start = min((i for i in (source.find("{"), source.find("[")) if i >= 0), default=-1)
    if start < 0:
        return None
    try:
        value, _ = json.JSONDecoder().raw_decode(source[start:])
        return value
    except json.JSONDecodeError:
        return None
