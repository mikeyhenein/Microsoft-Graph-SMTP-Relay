import pytest

from retry import DeliveryError, deliver_now, is_transient_status, parse_retry_after, queue_delay


def test_transient_statuses():
    for status in (401, 408, 429, 500, 502, 503, 504):
        assert is_transient_status(status)
    for status in (400, 403, 404, 413):
        assert not is_transient_status(status)


def test_retry_after_seconds_and_dates():
    assert parse_retry_after("12") == 12.0
    assert parse_retry_after(None) is None
    assert parse_retry_after("soon") is None
    # 2026-10-24 00:00:30 GMT is 30 seconds after the given "now"
    now = 1792800000.0
    assert parse_retry_after("Sat, 24 Oct 2026 00:00:30 GMT", now=now) == pytest.approx(30, abs=1)


def test_queue_backoff_schedule():
    assert [queue_delay(n) for n in range(0, 7)] == [60, 60, 300, 900, 1800, 3600, 3600]
    assert queue_delay(1, retry_after=600) == 600


class Clock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def time(self):
        return self.now

    async def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def flaky(*outcomes):
    """A send() that raises/returns the given outcomes in order."""
    calls = iter(outcomes)

    async def send():
        outcome = next(calls)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome
    return send


async def test_succeeds_after_two_transient_failures():
    clock = Clock()
    send = flaky(DeliveryError("503", transient=True), DeliveryError("503", transient=True), 202)
    assert await deliver_now(send, sleep=clock.sleep, clock=clock.time) == (202, 3)
    assert len(clock.sleeps) == 2


async def test_permanent_failure_is_not_retried():
    clock = Clock()
    with pytest.raises(DeliveryError) as caught:
        await deliver_now(flaky(DeliveryError("403", transient=False), 202), sleep=clock.sleep, clock=clock.time)
    assert caught.value.attempts == 1
    assert clock.sleeps == []


async def test_gives_up_after_three_tries():
    clock = Clock()
    send = flaky(*(DeliveryError("503", transient=True) for _ in range(5)))
    with pytest.raises(DeliveryError) as caught:
        await deliver_now(send, sleep=clock.sleep, clock=clock.time)
    assert caught.value.attempts == 3


async def test_retry_after_is_honoured_or_stops_when_over_budget():
    clock = Clock()
    send = flaky(DeliveryError("429", transient=True, retry_after=10), 202)
    assert await deliver_now(send, sleep=clock.sleep, clock=clock.time) == (202, 2)
    assert clock.sleeps[0] >= 10

    clock = Clock()
    send = flaky(DeliveryError("429", transient=True, retry_after=120), 202)
    with pytest.raises(DeliveryError) as caught:
        await deliver_now(send, sleep=clock.sleep, clock=clock.time)
    assert caught.value.attempts == 1  # waiting 120s would blow the 30s budget: hand off to the queue
