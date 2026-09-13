"""Does this endpoint actually serve two requests at once?

The Flock runs two Worker Birbs at a time by default, which is only true if
the thing behind the socket agrees. Ollama serves one request at a time unless
``OLLAMA_NUM_PARALLEL`` says otherwise, and a server that quietly queues the
second request turns a concurrent flock into a sequential one that *looks*
concurrent — two panes, one of them stalled, for reasons nothing tells you.

So CoBirb asks rather than assuming, and the answer is measured rather than
guessed. The measurement has one subtlety worth writing down, because the
obvious version does not work: **you cannot time this from when the requests
are sent.** The client fires both immediately, so the two intervals overlap
whether or not the server is doing anything with the second one.

What separates the cases is *when the first token comes back*:

    concurrent   A ├──────first token──────────────┤
                 B    ├───first token───────────┤        B answers while A still is

    serialised   A ├──────first token──────────────┤
                 B                                  ├──first token──┤

So the test is one line: **did B start answering before A finished?** That is
the user-facing description too — two tasks, timestamped, and you look at
whether the time frames overlap.

Nothing here is a performance judgement. Whether the endpoint serves one
request or eight is an observation about somebody's setup, and the only thing
CoBirb does with it is say so and offer to run sequentially.
"""
from __future__ import annotations

import concurrent.futures
import logging
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("cobirb")

# Long enough for two short completions on a model that is already resident.
# Loading a model is not counted against this — the warm-up below pays that
# first, so the probe measures the server's behaviour rather than its disk.
DEFAULT_TIMEOUT = 10

# Asks for enough tokens that a generation takes a measurable moment, without
# asking for anything worth reading.
_PROMPT = "Count slowly from one to twenty, one number per line."


@dataclass
class ProbeResult:
    """What the probe could tell, and how confident that is.

    ``concurrent`` is deliberately three-valued. ``None`` means the probe could
    not run — no model configured, the endpoint refused, streaming unsupported
    — and must never be read as either answer: telling someone their server is
    serial when CoBirb simply could not check would be worse than saying so.
    """

    concurrent: bool | None
    detail: str
    elapsed: float = 0.0

    def describe(self) -> str:
        if self.concurrent is None:
            return f"Could not tell whether the endpoint runs requests in parallel — {self.detail}"
        if self.concurrent:
            return f"The endpoint served two requests at once ({self.detail})."
        return (
            f"The endpoint runs one request at a time ({self.detail}). Worker Birbs would "
            "queue rather than run in parallel."
        )


def _timed_call(provider: Any, prompt: str) -> tuple[float, float]:
    """Run one request, returning when its first token arrived and when it ended.

    Streamed, because the first token is the whole measurement — a
    non-streaming call can only say how long the round trip took, which does
    not distinguish a server that queued the request from one that was simply
    slow.
    """
    started = time.monotonic()
    first = 0.0
    stream = provider.chat("", prompt, None, stream=True)
    for chunk in stream:
        if chunk and not first:
            first = time.monotonic()
    if not first:
        first = time.monotonic()
    return first - started, time.monotonic() - started


def probe_concurrency(
    provider: Any, *, timeout: int = DEFAULT_TIMEOUT, prompt: str = _PROMPT
) -> ProbeResult:
    """Find out whether ``provider``'s endpoint answers two requests at once.

    Warms up first, with one throwaway request. That call pays for loading the
    model, which on a large one dwarfs everything this is trying to measure and
    would otherwise make every endpoint look serial.

    Never raises. A probe is a convenience before a flock run; an endpoint that
    cannot be probed is one CoBirb says it could not probe, not a reason to
    refuse the run.
    """
    if not getattr(provider, "supports_streaming", lambda: False)():
        return ProbeResult(None, "this provider does not stream, so there is nothing to time")

    started = time.monotonic()
    try:
        _timed_call(provider, "Say OK.")  # warm-up: pays for the model load
    except Exception as exc:  # noqa: BLE001 - an unreachable endpoint is an answer of sorts
        return ProbeResult(None, f"the endpoint could not be reached — {exc}")

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(_timed_call, provider, prompt) for _ in range(2)]
            (first_a, done_a), (first_b, done_b) = [
                future.result(timeout=timeout) for future in futures
            ]
    except concurrent.futures.TimeoutError:
        return ProbeResult(None, f"neither request finished within {timeout}s")
    except Exception as exc:  # noqa: BLE001
        return ProbeResult(None, f"the probe itself failed — {exc}")

    elapsed = time.monotonic() - started
    # Both were fired together, so these are all measured from the same zero.
    # The later one beginning to answer before the earlier one finished is the
    # whole signal.
    later_first_token = max(first_a, first_b)
    earlier_finished = min(done_a, done_b)
    overlapped = later_first_token < earlier_finished

    detail = (
        f"second reply began at {later_first_token:.2f}s, first finished at "
        f"{earlier_finished:.2f}s"
    )
    logger.info("concurrency probe: overlapped=%s (%s)", overlapped, detail)
    return ProbeResult(overlapped, detail, elapsed)
