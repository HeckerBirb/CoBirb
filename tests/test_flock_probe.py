"""Tests for the endpoint concurrency probe.

Two fake servers: one that answers both callers at once, one that holds a lock
so the second only begins when the first has finished. The probe has to tell
them apart from timing alone, because that is all it ever gets.
"""
from __future__ import annotations

import threading
import time

from cobirb.flock.probe import probe_concurrency


class _Server:
    """A provider that streams a few chunks, with a settable degree of parallelism."""

    def __init__(self, *, parallel: bool, chunk_delay: float = 0.05, chunks: int = 6):
        self.parallel = parallel
        self.chunk_delay = chunk_delay
        self.chunks = chunks
        self._lock = threading.Lock()
        self.calls = 0

    def supports_streaming(self):
        return True

    def chat(self, system, context, tools=None, *, stream=False):
        self.calls += 1
        return self._stream()

    def _stream(self):
        if self.parallel:
            yield from self._chunks()
        else:
            # One at a time: the second caller sees nothing at all until the
            # first has finished, which is exactly what a server with
            # OLLAMA_NUM_PARALLEL=1 does.
            with self._lock:
                yield from self._chunks()

    def _chunks(self):
        for _ in range(self.chunks):
            time.sleep(self.chunk_delay)
            yield "x"


def test_a_parallel_endpoint_is_recognised():
    result = probe_concurrency(_Server(parallel=True))

    assert result.concurrent is True
    assert "served two requests at once" in result.describe()


def test_a_serialising_endpoint_is_recognised():
    """The case worth catching: two panes, one of them stalled, for reasons
    nothing tells you."""
    result = probe_concurrency(_Server(parallel=False))

    assert result.concurrent is False
    assert "one request at a time" in result.describe()
    assert "queue rather than run in parallel" in result.describe()


def test_the_model_load_is_not_counted_against_the_verdict():
    """A warm-up call pays for loading the model first. Without it a large
    model makes every endpoint look serial, because the first request carries
    a cost the second never pays."""
    class _SlowFirstCall(_Server):
        def chat(self, *args, **kwargs):
            first = self.calls == 0
            self.calls += 1
            return self._loading() if first else self._stream()

        def _loading(self):
            time.sleep(0.4)  # "loading the model"
            yield "ok"

    result = probe_concurrency(_SlowFirstCall(parallel=True))

    assert result.concurrent is True


def test_a_provider_that_cannot_stream_is_not_guessed_about():
    """Saying "your server is serial" when CoBirb simply could not check would
    be worse than saying so."""
    class _NoStreaming:
        def supports_streaming(self):
            return False

    result = probe_concurrency(_NoStreaming())

    assert result.concurrent is None
    assert "Could not tell" in result.describe()


def test_an_unreachable_endpoint_is_reported_not_raised():
    """A probe is a convenience before a flock run, never a reason to refuse
    one."""
    class _Broken:
        def supports_streaming(self):
            return True

        def chat(self, *args, **kwargs):
            raise RuntimeError("connection refused")

    result = probe_concurrency(_Broken())

    assert result.concurrent is None
    assert "connection refused" in result.detail


def test_a_probe_that_never_finishes_gives_up():
    # The endpoint stalls for longer than the probe is willing to wait, which
    # is the whole condition under test — a few hundred milliseconds proves it
    # exactly as well as a few seconds would.
    class _Hanging(_Server):
        def _chunks(self):
            time.sleep(0.6)
            yield "x"

    result = probe_concurrency(_Hanging(parallel=True), timeout=0.2)

    assert result.concurrent is None
    assert "within 0.2s" in result.detail
