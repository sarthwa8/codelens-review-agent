"""Worker side of the live stream: append review tokens to a Redis Stream.

Why Streams rather than pub/sub: pub/sub only delivers to subscribers connected at publish time.
A browser that opens a review mid-generation, reconnects after a network blip, or opens a cache
hit that finished in milliseconds would see partial or no output. A Stream is an append-only log,
so the SSE endpoint can replay from the beginning (or from ``Last-Event-ID``) and then tail it.

Tokens are coalesced into ~50 ms batches: still visibly incremental in the UI, but one XADD per
batch rather than per token.
"""

import json
import time
from collections.abc import Callable
from typing import Any

import redis


def stream_key(result_id: int) -> str:
    return f"codelens:stream:{result_id}"


class StreamPublisher:
    MAXLEN = 20_000  # safety cap on entries per stream

    def __init__(
        self,
        client: redis.Redis,
        result_id: int,
        *,
        flush_interval_ms: int,
        ttl_seconds: int,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._redis = client
        self._key = stream_key(result_id)
        self._interval = flush_interval_ms / 1000
        self._ttl = ttl_seconds
        self._clock = clock
        self._buffer: list[str] = []
        self._last_flush = clock()

    def _emit(self, event_type: str, data: dict[str, Any]) -> None:
        pipe = self._redis.pipeline(transaction=False)
        pipe.xadd(
            self._key,
            {"type": event_type, "data": json.dumps(data)},
            maxlen=self.MAXLEN,
            approximate=True,
        )
        pipe.expire(self._key, self._ttl)  # abandoned streams clean themselves up
        pipe.execute()

    def start(self, attempt: int) -> None:
        # A retry restarts generation from scratch. Drop the previous attempt's entries and tell
        # connected clients to clear any partial text they already rendered.
        self._redis.delete(self._key)
        self._emit("reset", {"attempt": attempt})
        self._emit("status", {"status": "streaming"})
        self._last_flush = self._clock()

    def write(self, text: str) -> None:
        self._buffer.append(text)
        if self._clock() - self._last_flush >= self._interval:
            self.flush()

    def flush(self) -> None:
        if self._buffer:
            self._emit("delta", {"text": "".join(self._buffer)})
            self._buffer.clear()
        self._last_flush = self._clock()

    def done(self, **meta: Any) -> None:
        self.flush()
        self._emit("done", {"status": "complete", **meta})

    def failed(self, message: str, *, retrying: bool) -> None:
        self.flush()
        if retrying:
            self._emit("retrying", {"message": message})
        else:
            self._emit("failed", {"message": message})
