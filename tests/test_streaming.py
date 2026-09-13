import asyncio
import json
import threading
import time
from dataclasses import replace

import fakeredis
import pytest

from app.config import get_settings
from app.streaming import sse
from app.streaming.publisher import StreamPublisher, stream_key
from app.streaming.sse import ReviewState, review_events

RESULT_ID = 7


class FakeRequest:
    def __init__(self) -> None:
        self.disconnected = False

    async def is_disconnected(self) -> bool:
        return self.disconnected


def parse(raw: list[str]) -> list[dict]:
    events = []
    for block in raw:
        if block.startswith((":", "retry:")):
            continue
        fields = dict(line.split(": ", 1) for line in block.strip().splitlines())
        events.append(
            {"id": fields.get("id"), "event": fields["event"], "data": json.loads(fields["data"])}
        )
    return events


async def collect(generator, limit_seconds: float = 5.0) -> list[dict]:
    raw: list[str] = []

    async def run() -> None:
        async for chunk in generator:
            raw.append(chunk)

    await asyncio.wait_for(run(), limit_seconds)
    return parse(raw)


def text_of(events: list[dict]) -> str:
    text = ""
    for e in events:
        if e["event"] == "reset":
            text = ""
        elif e["event"] == "delta":
            text += e["data"]["text"]
        elif e["event"] == "snapshot":
            text = e["data"]["text"]
    return text


def state(**overrides) -> ReviewState:
    base = ReviewState(
        status="streaming", result_id=RESULT_ID, result_status="streaming", text="", cache_hit=False,
        skip_reason=None, error=None, latency_ms=None, output_tokens=None,
    )  # fmt: skip
    return replace(base, **overrides)


@pytest.fixture
def server() -> fakeredis.FakeServer:
    return fakeredis.FakeServer()


@pytest.fixture
def publisher(server) -> StreamPublisher:
    client = fakeredis.FakeRedis(server=server, decode_responses=True)
    return StreamPublisher(client, RESULT_ID, flush_interval_ms=0, ttl_seconds=60)


@pytest.fixture
def aredis(server):
    return fakeredis.FakeAsyncRedis(server=server, decode_responses=True)


@pytest.fixture
def settings():
    return get_settings().model_copy(update={"sse_heartbeat_seconds": 1})


def test_publisher_batches_tokens_within_flush_interval(server) -> None:
    client = fakeredis.FakeRedis(server=server, decode_responses=True)
    now = [0.0]
    pub = StreamPublisher(client, 1, flush_interval_ms=50, ttl_seconds=60, clock=lambda: now[0])
    pub.start(attempt=1)
    for token in ["a", "b", "c"]:
        pub.write(token)  # same instant → buffered
    now[0] = 0.06
    pub.write("d")  # interval elapsed → one XADD containing "abcd"
    pub.done(latency_ms=5)
    types = [fields["type"] for _, fields in client.xrange(stream_key(1))]
    assert types == ["reset", "status", "delta", "done"]
    deltas = [
        json.loads(f["data"])["text"]
        for _, f in client.xrange(stream_key(1))
        if f["type"] == "delta"
    ]
    assert deltas == ["abcd"]
    assert 0 < client.ttl(stream_key(1)) <= 60


async def test_late_joiner_replays_everything(publisher, aredis, settings) -> None:
    publisher.start(attempt=1)
    for token in ["### Summary\n", "Looks ", "good."]:
        publisher.write(token)
    publisher.done(latency_ms=12)

    events = await collect(
        review_events(1, FakeRequest(), aredis, settings, None, loader=lambda _: state())
    )
    assert text_of(events) == "### Summary\nLooks good."
    assert events[-1]["event"] == "done"
    assert all(e["id"] for e in events)  # every replayed event carries a resumable id


async def test_last_event_id_resumes_without_duplicates(publisher, aredis, settings) -> None:
    publisher.start(attempt=1)
    for token in ["one ", "two ", "three"]:
        publisher.write(token)
    publisher.done()
    full = await collect(
        review_events(1, FakeRequest(), aredis, settings, None, loader=lambda _: state())
    )
    resume_after = next(e for e in full if e["event"] == "delta")["id"]

    resumed = await collect(
        review_events(1, FakeRequest(), aredis, settings, resume_after, loader=lambda _: state())
    )
    assert [e["data"].get("text") for e in resumed if e["event"] == "delta"] == ["two ", "three"]


async def test_live_tail_receives_tokens_as_they_are_published(publisher, aredis, settings) -> None:
    publisher.start(attempt=1)
    arrival: list[float] = []

    def produce() -> None:
        for token in ["a", "b", "c"]:
            time.sleep(0.1)
            publisher.write(token)
            arrival.append(time.monotonic())
        publisher.done()

    thread = threading.Thread(target=produce)
    thread.start()
    events = await collect(
        review_events(1, FakeRequest(), aredis, settings, None, loader=lambda _: state())
    )
    thread.join()
    assert text_of(events) == "abc"


async def test_completed_review_is_served_from_database_as_snapshot(aredis, settings) -> None:
    done = state(
        status="complete",
        result_status="complete",
        text="cached review",
        cache_hit=True,
        latency_ms=900,
    )
    events = await collect(
        review_events(1, FakeRequest(), aredis, settings, None, loader=lambda _: done)
    )
    assert [e["event"] for e in events] == ["snapshot", "done"]
    assert events[0]["data"] == {"text": "cached review", "cache_hit": True}


async def test_skipped_and_failed_reviews_terminate(aredis, settings) -> None:
    skipped = state(
        status="skipped", result_id=None, result_status=None, skip_reason="generated lockfile"
    )
    events = await collect(
        review_events(1, FakeRequest(), aredis, settings, None, loader=lambda _: skipped)
    )
    assert [e["event"] for e in events] == ["skipped", "done"]

    failed = state(status="failed", result_status="failed", error="model overloaded")
    events = await collect(
        review_events(1, FakeRequest(), aredis, settings, None, loader=lambda _: failed)
    )
    assert events == [{"id": None, "event": "failed", "data": {"message": "model overloaded"}}]


async def test_queued_review_waits_for_a_result(publisher, aredis, settings, monkeypatch) -> None:
    monkeypatch.setattr(sse, "QUEUED_POLL_SECONDS", 0.01)
    states = iter([state(status="pending", result_id=None, result_status=None)] * 3)

    def loader(_):
        return next(states, state())

    publisher.start(attempt=1)
    publisher.write("hi")
    publisher.done()
    events = await collect(review_events(1, FakeRequest(), aredis, settings, None, loader=loader))
    assert events[0] == {"id": None, "event": "status", "data": {"status": "queued"}}
    assert text_of(events) == "hi"


async def test_retry_reset_clears_partial_text(publisher, aredis, settings) -> None:
    publisher.start(attempt=1)
    publisher.write("partial garbage")
    publisher.failed("overloaded", retrying=True)
    publisher.start(attempt=2)  # deletes the old attempt and emits reset
    publisher.write("clean review")
    publisher.done()
    events = await collect(
        review_events(1, FakeRequest(), aredis, settings, None, loader=lambda _: state())
    )
    assert text_of(events) == "clean review"


async def test_stream_lost_but_result_completed_falls_back_to_database(aredis, settings) -> None:
    calls = iter([state(), state(status="complete", result_status="complete", text="from db")])
    events = await collect(
        review_events(1, FakeRequest(), aredis, settings, None, loader=lambda _: next(calls)),
        limit_seconds=5,
    )
    assert text_of(events) == "from db"
