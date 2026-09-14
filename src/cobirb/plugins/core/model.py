"""Core model provider: a local Ollama-compatible chat backend.

CoBirb ships **no models** and makes **no outbound network calls by default**.
This provider only ever talks to a *local* Ollama server (default
``http://localhost:11434``, or wherever the user points it) and only once the
user has explicitly named a model — via ``--model``, ``COBIRB_MODEL_NAME``, or
``models.default.name`` in config.

**The model's own prompt wins.** Ollama takes one system message per request,
and sending one *replaces* the ``SYSTEM`` directive the model was built with.
A model created with ``ollama create`` around a custom ``SYSTEM`` is a
configuration its user made deliberately, so CoBirb defaults to sending no
system message at all and letting that directive apply untouched. When CoBirb
does have something to add — a persona, plan-mode phase instructions — the
model's own prompt is read back via ``/api/show`` and placed first, so the
addition supplements it instead of discarding it. See ``compose_system``.
"""
from __future__ import annotations

import http.client
import io
import json
import os
import socket
import threading
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Iterable, Optional

from ...typing.spi import ModelProvider, SteeringInterrupted, Tool, ToolCall

DEFAULT_BASE_URL = "http://localhost:11434"


def _tool_schema(tool: Tool) -> dict[str, Any]:
    """Convert a CoBirb Tool into the function-calling schema Ollama expects."""
    return {
        "type": "function",
        "function": {
            "name": tool.name(),
            "description": tool.description(),
            "parameters": tool.parameters(),
        },
    }


def _extract_tool_calls(message: dict[str, Any]) -> list[ToolCall]:
    """Map Ollama's tool_calls shape onto CoBirb ToolCall objects."""
    return [
        ToolCall(name=call["function"]["name"], arguments=call["function"].get("arguments", {}) or {})
        for call in (message.get("tool_calls") or [])
    ]


def _num_ctx(parameters: Any) -> int | None:
    """Pull ``num_ctx`` out of ``/api/show``'s ``parameters`` block.

    Ollama returns those as one newline-separated string of ``name value``
    pairs rather than as JSON, so this reads it as text.
    """
    if not isinstance(parameters, str):
        return None
    for line in parameters.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] == "num_ctx":
            try:
                value = int(parts[1])
            except ValueError:
                return None
            return value if value > 0 else None
    return None


def _advertised_context(model_info: Any) -> int | None:
    """The context length the model itself declares.

    Stored under an architecture-prefixed key — ``llama.context_length``,
    ``qwen2.context_length`` — so the suffix is what identifies it rather than
    any fixed name.
    """
    if not isinstance(model_info, dict):
        return None
    for key, value in model_info.items():
        if str(key).endswith(".context_length"):
            try:
                length = int(value)
            except (TypeError, ValueError):
                return None
            return length if length > 0 else None
    return None


def _build_messages(system: str, context: str) -> list[dict[str, Any]]:
    """Turn the orchestrator's JSON-encoded turn history into a proper
    multi-turn Ollama ``messages`` array, instead of flattening the whole
    conversation into a single opaque "user" message.

    That flattening was the root cause of the tool-calling loop never
    converging: a "tool" result appeared out of nowhere, with no preceding
    assistant message announcing the tool call it answers, so the model had
    no signal a prior call was already satisfied and would just repeat it.
    The SPI leaves ``context`` as a compact string and makes each provider
    responsible for how it unpacks it; here that packing is JSON, parsed back
    into role-tagged messages.

    An empty ``system`` produces **no system message at all**, rather than an
    empty one. That distinction is the whole of ``respect_model_system``: an
    explicit ``{"role": "system"}`` entry — even a blank one — replaces the
    ``SYSTEM`` directive from the model's own Modelfile for that request,
    while omitting the entry lets Ollama apply the model's own.
    """
    messages: list[dict[str, Any]] = []
    if system:
        messages.append({"role": "system", "content": system})
    try:
        turns = json.loads(context) if context else []
    except (json.JSONDecodeError, TypeError):
        turns = None

    if not isinstance(turns, list):
        # Not the JSON shape this provider expects (e.g. a hand-built plain
        # string context) — fall back to a single opaque user message rather
        # than dropping it, so the provider still works with any string.
        if context:
            messages.append({"role": "user", "content": context})
        return messages

    for turn in turns:
        role = turn.get("role", "user")
        content = turn.get("content", "")
        tool_use = turn.get("tool_use")
        if role == "assistant" and tool_use:
            messages.append(
                {
                    "role": "assistant",
                    "content": content,
                    "tool_calls": [
                        {"function": {"name": tu["name"], "arguments": tu.get("arguments", {})}}
                        for tu in tool_use
                    ],
                }
            )
        elif role == "tool":
            message: dict[str, Any] = {"role": "tool", "content": content}
            if tool_use:
                message["tool_name"] = tool_use[0].get("name", "")
            messages.append(message)
        else:
            messages.append({"role": role if role == "assistant" else "user", "content": content})
    return messages


def _unreachable(base_url: str, exc: Exception, model: str = "") -> RuntimeError:
    """Turn a failed request into something that names the actual problem.

    ``urllib.error.HTTPError`` is a *subclass* of ``URLError``, so a single
    ``except URLError`` catches both "nothing is listening" and "the server
    answered, and the answer was no" — and reports them identically. That is
    how a model the server does not have came back as "Is Ollama running?",
    with the real reason (Ollama says ``model "x" not found, try pulling it
    first``) sitting unread in the response body.

    So: a transport failure keeps the old message, because "is it running" is
    the right question then. An HTTP status is reported as what it is, with
    the server's own words, because the server has already said what is wrong
    and repeating it beats guessing.
    """
    if isinstance(exc, urllib.error.HTTPError):
        detail = _error_body(exc)
        named = f" {model!r}" if model else " that model"
        if exc.code == 404:
            return RuntimeError(
                f"The model provider at {base_url} does not have{named}"
                f"{f' — it said: {detail}' if detail else ''}. "
                "Check the spelling with 'cobirb models', and that it is pulled "
                "('ollama list')."
            )
        return RuntimeError(
            f"The model provider at {base_url} refused the request for{named}: "
            f"{exc.code} {exc.reason}{f' — {detail}' if detail else ''}"
        )
    return RuntimeError(
        f"Could not reach the model provider at {base_url}: {exc}. Is Ollama running?"
    )


def _error_body(exc: "urllib.error.HTTPError") -> str:
    """Whatever the server said about the failure, if it said anything.

    Ollama puts a plain message under ``error``; other OpenAI-compatible
    servers nest it under ``error.message``. Both are worth more than the
    status line, and a body that is neither is returned as-is rather than
    dropped.
    """
    try:
        raw = exc.read().decode("utf-8", "replace").strip()
    except Exception:  # noqa: BLE001 - a body we cannot read is simply absent
        return ""
    if not raw:
        return ""
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return raw[:300]
    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict):
        error = error.get("message")
    return str(error or raw)[:300]


class LocalModelProvider(ModelProvider):
    """Chats with a local Ollama server.

    No inference happens unless a model name is supplied; there is no
    embedded model and no default remote endpoint.
    """

    def __init__(self, model: str = "", base_url: str | None = None, cwd: str | None = None) -> None:
        self._model = model or os.environ.get("COBIRB_MODEL_NAME", "")
        self._base_url = (base_url or os.environ.get("COBIRB_OLLAMA_URL") or DEFAULT_BASE_URL).rstrip("/")
        self.cwd = cwd or os.getcwd()
        # Ollama returns structured tool calls alongside the assistant message
        # in the same response; stash them here so parse_tool_calls() doesn't
        # need to re-parse text or make a second round trip.
        self._last_tool_calls: list[ToolCall] = []
        # Modelfile SYSTEM directives, keyed by model name. A model's own
        # prompt doesn't change between requests, so this is fetched once per
        # model rather than on every turn. Only successes are cached: a
        # failed lookup returns "" without being remembered, so a transient
        # blip doesn't permanently drop the user's own prompt.
        # One /api/show payload per model, shared by everything that needs
        # something out of it — the Modelfile SYSTEM directive and the context
        # window both live there, and fetching it twice for one model would be
        # two round trips to learn one thing.
        self._show_cache: dict[str, dict[str, Any]] = {}
        # In-flight streaming responses, so a run can be cut off from another
        # thread — the Flock's force-stop. Closing the connection is also what
        # tells Ollama to abort the generation it is part-way through: the
        # server notices the client is gone and stops, rather than finishing a
        # reply nobody is waiting for.
        self._inflight: set = set()
        self._inflight_lock = threading.Lock()
        self._cancelled = False
        # Set by interrupt_current_reply() to cut off *this* stream without
        # latching the provider closed the way `_cancelled` does — see that
        # method and `_as_control_exception` for the distinction that matters.
        self._steer_signal = threading.Event()

    def name(self) -> str:
        return f"ollama/{self._model}" if self._model else "(unconfigured)"

    def _connect(self) -> tuple[http.client.HTTPConnection, str]:
        """A tracked connection to the endpoint, plus the URL's path prefix.

        Used by ``_stream_chat`` only — not by ``_post``, which stays on
        ``urllib`` (see its docstring for why). ``http.client`` rather than
        ``urllib`` here for one reason: keeping a handle on the real socket, so
        ``cancel()`` can shut it down from another thread. ``urllib`` hides the
        socket, and closing a response from another thread does not wake a
        blocked ``recv`` on Linux; only ``socket.shutdown()`` does.

        Raises immediately if ``cancel()`` already fired, so a request that has
        not opened yet does not sail on regardless.
        """
        split = urllib.parse.urlsplit(self._base_url)
        connection_class = (
            http.client.HTTPSConnection if split.scheme == "https"
            else http.client.HTTPConnection
        )
        conn = connection_class(split.hostname, split.port, timeout=120)
        with self._inflight_lock:
            if self._cancelled:
                raise RuntimeError("the model request was cancelled")
            self._inflight.add(conn)
        return conn, split.path

    def _release(self, conn: http.client.HTTPConnection) -> None:
        with self._inflight_lock:
            self._inflight.discard(conn)
        conn.close()

    def _as_control_exception(self, exc: Exception) -> Exception:
        """Whatever this failure actually was, report it as *our own doing*
        if a ``cancel()`` or ``interrupt_current_reply()`` is why it happened.

        A shutdown socket fails in whatever way the code that was reading it
        happens to fail — ``http.client`` hits EOF, nulls its file pointer, and
        the next read raises ``AttributeError`` rather than a clean ``OSError``.
        The type is not the interesting fact once we know we did this to
        ourselves; the interesting fact is *which* of the two we did.
        ``cancel()`` wins if somehow both fired, since a full stop subsumes a
        steer.
        """
        if self._cancelled:
            return RuntimeError("the model request was cancelled")
        if self._steer_signal.is_set():
            self._steer_signal.clear()
            return SteeringInterrupted()
        return exc

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        """A single request/response round trip — ``/api/show`` and the
        non-streaming ``chat()``.

        Deliberately still ``urllib``, and **not** part of ``cancel()``'s
        reach. Moving it to the same tracked-socket scheme as ``_stream_chat``
        would close a real but narrow gap — ``/api/show`` is called before
        every ``chat()``, so a hang there is currently not force-stoppable —
        at the cost of rewriting every one of this file's ~30 existing tests,
        which all mock ``urllib.request.urlopen`` at this exact call. That is a
        real trade, not an oversight: see AGENTS.md's Flock section for the
        note this leaves for whoever picks it up. In practice this rarely
        matters — every Worker Birb turn goes through ``_stream_chat`` (real
        streaming is on whenever an ``io`` adapter is attached and the
        provider supports it, which ``HeadlessIO`` plus this provider always
        are), and ``/api/show`` is a fast local metadata call that only hangs
        if the whole server is already wedged, not merely mid-generation.
        """
        data = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            f"{self._base_url}{path}",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.URLError as exc:
            raise _unreachable(self._base_url, exc, self._model) from exc

    def list_models(self) -> list[str]:
        """Return the model names available from the configured endpoint.

        Queries the OpenAI-compatible ``GET /v1/models`` route rather than
        Ollama's own ``/api/tags`` — Ollama serves both, but the OpenAI-style
        route is also what other self-hosted, OpenAI-compatible servers
        (llama.cpp, vLLM, LM Studio, ...) expose, so this works unmodified if
        ``base_url`` ever points somewhere other than Ollama. Used by
        interactive mode's ``/model`` picker and its startup model check —
        never called from the one-shot/programmatic path.
        """
        request = urllib.request.Request(
            f"{self._base_url}/v1/models", headers={"Accept": "application/json"}
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.URLError as exc:
            raise _unreachable(self._base_url, exc) from exc
        entries = payload.get("data") or []
        return sorted({entry["id"] for entry in entries if entry.get("id")})

    def model_system_prompt(self) -> str:
        """The ``SYSTEM`` directive baked into this model's own Modelfile.

        Returns ``""`` for a model that declares none — which is most of
        them — and for any lookup that fails. Best-effort context, never a
        precondition for chatting: if the endpoint can't answer, the turn
        still runs, it just can't prepend a prompt it couldn't read.
        """
        system = self._show().get("system") or ""
        return system if isinstance(system, str) else ""

    def context_window(self) -> int | None:
        """The context window to use with this model, or ``None`` if unknown.

        Optional and duck-typed, the same way ``list_models`` is.

        **CoBirb asks for a window rather than guessing at one.** Ollama uses
        its own modest default (4096) when a Modelfile doesn't set ``num_ctx``,
        and an earlier version of this therefore refused to believe a model's
        advertised ``context_length`` — packing a request against 131072 that
        the server would then silently truncate is exactly the failure
        ``cobirb.context`` exists to prevent.

        That was solving the wrong problem. ``/api/chat`` accepts
        ``options.num_ctx``, so the window is not something to discover
        passively: ``chat`` states it on every request and the server honours
        it. What this resolves is therefore what to *ask for* — the Modelfile's
        own ``num_ctx`` if its author chose one, otherwise what the model says
        it can do.
        """
        payload = self._show()
        # A Modelfile naming num_ctx is a deliberate choice by whoever built
        # the model, and outranks the architecture's maximum.
        return _num_ctx(payload.get("parameters")) or _advertised_context(payload.get("model_info"))

    def _show(self) -> dict[str, Any]:
        """This model's ``/api/show`` payload, fetched once and remembered.

        Returns ``{}`` for anything that goes wrong — an unreachable endpoint,
        a model the server doesn't know, a payload of an unexpected shape.
        None of those should stop the turn the user actually asked for; they
        only mean this request can't carry information it couldn't read. A
        failure is deliberately *not* cached, so a transient blip doesn't
        permanently cost the session its context window and system prompt.
        """
        if not self._model:
            return {}
        if self._model in self._show_cache:
            return self._show_cache[self._model]
        try:
            payload = self._post("/api/show", {"model": self._model})
        except Exception:  # noqa: BLE001 - best-effort, never fatal
            return {}
        if not isinstance(payload, dict):
            return {}
        self._show_cache[self._model] = payload
        return payload

    def compose_system(self, system: str) -> str:
        """Combine CoBirb's own system prompt with the model's, if any.

        The rule, in order:

        - CoBirb has nothing to say (``system`` empty — the default, with no
          persona and no harness block): return ``""``, which sends **no**
          system message and leaves the model's own Modelfile ``SYSTEM``
          doing exactly what it does outside CoBirb. This is the case that
          matters: a client that injects its own prompt on every request
          silently overrides the model its user configured, and a model
          built with ``ollama create`` around a custom ``SYSTEM`` is that
          configuration.
        - CoBirb has something to say and the model declares no ``SYSTEM``:
          send CoBirb's, since there is nothing to displace.
        - Both: the model's own goes **first**, CoBirb's after it. Ollama
          accepts one system message per request, so "respecting" the
          model's prompt when something must be added means carrying it
          into the message rather than dropping it — and putting it first
          means CoBirb's additions read as a supplement to it rather than a
          replacement of it.
        """
        if not system.strip():
            return ""
        own = self.model_system_prompt().strip()
        if not own:
            return system
        return f"{own}\n\n{system}"

    def chat(
        self,
        system: str,
        context: str,
        tools: Optional[list[Tool]] = None,
        *,
        stream: bool = False,
    ) -> "Iterable[str] | str":
        if not self._model:
            raise RuntimeError(
                "No model configured. Set --model, COBIRB_MODEL_NAME, or "
                "\"default_model\"/models.default.name in your CoBirb config — "
                "or, in interactive mode, pick one with /model. No models are "
                "embedded by default."
            )
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": _build_messages(self.compose_system(system), context),
            "stream": stream,
        }
        # State the window rather than hoping the server's default is
        # generous. Without this, Ollama serves 4096 regardless of what the
        # model can do, and a long conversation is truncated from the front
        # with nobody told.
        window = self.context_window()
        if window:
            payload["options"] = {"num_ctx": window}
        if tools:
            payload["tools"] = [_tool_schema(t) for t in tools]

        if stream:
            return self._stream_chat(payload)

        response = self._post("/api/chat", payload)
        message = response.get("message", {})
        self._last_tool_calls = _extract_tool_calls(message)
        return message.get("content", "")

    def _stream_chat(self, payload: dict[str, Any]) -> Iterable[str]:
        """Yield content deltas as Ollama streams them (NDJSON response body).

        Each line is a complete JSON object for one increment; the last one
        has ``"done": true`` and carries any tool calls the model decided to
        make. ``_last_tool_calls`` is only accurate once the generator has
        been fully consumed.

        Shares ``_connect``/``_release`` with ``_post`` — see ``_connect`` for
        why a tracked ``http.client`` socket, rather than ``urllib``, is what
        makes a stuck Worker Birb force-stoppable at all.
        """
        conn, prefix = self._connect()
        tool_calls: list[ToolCall] = []
        try:
            try:
                conn.request(
                    "POST",
                    f"{prefix}/api/chat",
                    body=json.dumps(payload).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                )
                response = conn.getresponse()
            except OSError as exc:
                if self._cancelled or self._steer_signal.is_set():
                    raise self._as_control_exception(exc) from exc
                raise _unreachable(self._base_url, urllib.error.URLError(exc), self._model) from exc
            if response.status >= 400:
                body = response.read()
                raise _unreachable(
                    self._base_url,
                    urllib.error.HTTPError(
                        self._base_url, response.status, response.reason, response.headers,
                        io.BytesIO(body),
                    ),
                    self._model,
                )
            for raw_line in response:
                line = raw_line.strip()
                if not line:
                    continue
                chunk = json.loads(line)
                message = chunk.get("message", {})
                content = message.get("content", "")
                if content:
                    yield content
                if message.get("tool_calls"):
                    tool_calls = _extract_tool_calls(message)
                if chunk.get("done"):
                    break
        except Exception as exc:  # noqa: BLE001 - see _as_control_exception
            raise self._as_control_exception(exc) from exc
        finally:
            self._release(conn)
        self._last_tool_calls = tool_calls

    def cancel(self) -> None:
        """Abort any request in flight, and refuse any that starts after.

        Reaches every connection this provider has open — ``/api/show``, a
        non-streaming ``/api/chat``, or the streaming one, whichever a stuck
        Worker Birb happens to be blocked on; ``chat()`` calls the first before
        either of the others, so a hung server can wedge a turn there just as
        easily. Shutting the socket unblocks the reader waiting on it — it
        raises there, which propagates as a failed turn — and drops the
        connection, which is how Ollama is told to stop generating. There is no
        per-request abort endpoint; the disconnect *is* the signal.

        Latching ``_cancelled`` means a request that has not opened its socket
        yet (between the decision to cancel and ``_connect()`` returning) is
        refused rather than sailing on. This is a one-way switch: a cancelled
        provider is a stopped one, which is exactly what a force-stop wants.
        """
        with self._inflight_lock:
            self._cancelled = True
            connections = list(self._inflight)
        for conn in connections:
            # shutdown(), not close(): closing a socket from another thread
            # does not wake a blocked recv, but shutting it down does. This is
            # the line that actually unsticks a worker waiting on the model —
            # and, because the connection drops, tells the server to stop.
            sock = getattr(conn, "sock", None)
            if sock is not None:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            try:
                conn.close()
            except Exception:  # noqa: BLE001 - already going; nothing to do
                pass

    def interrupt_current_reply(self) -> bool:
        """Cut off whatever this provider is streaming right now — mid-turn
        steering's resumable counterpart to ``cancel()``'s one-way stop.

        Shuts down the same tracked socket ``cancel()`` does, for the same
        reason (only ``socket.shutdown()``, not ``close()``, wakes a ``recv``
        blocked on another thread — see ``cancel()``). The difference is
        entirely in what happens *after*: ``cancel()`` latches ``_cancelled``
        so every later request is refused; this sets ``_steer_signal``
        instead, which ``_as_control_exception`` consumes exactly once (on
        the very interruption it caused) and clears — so the next ``chat()``
        call opens a fresh connection and runs as if nothing happened. A
        provider that is mid-generation when this fires raises
        ``SteeringInterrupted`` out of ``_stream_chat``; a provider with
        nothing in flight (between tool calls, or not yet connected) has
        nothing to interrupt, hence the return value.

        Not part of ``chat()``/non-streaming: ``_post`` stays on plain
        ``urllib`` for the reasons its own docstring gives, so a steer that
        lands while ``/api/show`` or a non-streaming call is in flight simply
        has nothing to cut off here — the orchestrator still applies the
        queued steering message at the next loop boundary either way.
        """
        with self._inflight_lock:
            connections = list(self._inflight)
            if connections:
                self._steer_signal.set()
        for conn in connections:
            # Both shutdown() and close(), exactly as cancel() does: shutdown()
            # alone wakes the blocked recv but a shutdown-but-not-closed socket
            # can still read a clean EOF and let the chunked-response parser
            # end the stream quietly, with no exception for _as_control_exception
            # to turn into SteeringInterrupted. Closing the connection on top
            # is what actually makes the read fail loudly.
            sock = getattr(conn, "sock", None)
            if sock is not None:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            try:
                conn.close()
            except Exception:  # noqa: BLE001 - already going; nothing to do
                pass
        return bool(connections)

    def parse_tool_calls(self, raw: str) -> list[ToolCall]:
        return self._last_tool_calls

    def supports_tool_calling(self) -> bool:
        return bool(self._model)

    def supports_streaming(self) -> bool:
        return True

    def supports_vision(self) -> bool:
        return False
