"""Which Graph failures are worth retrying, and how long to wait between tries."""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from email.utils import parsedate_to_datetime

# Statuses Microsoft documents as "try again later", plus 401 (an expired token
# gets replaced on the next try). Every other 4xx means the message itself was refused.
TRANSIENT_STATUSES = {401, 408, 425, 429}

# In the SMTP session: tries at 0s, ~1s and ~4s later, within a 30s budget.
IN_SESSION_DELAYS = (1.0, 4.0)
IN_SESSION_BUDGET = 30.0

# Background queue: wait 1m, 5m, 15m, 30m, then every hour.
QUEUE_BACKOFF = (60, 300, 900, 1800, 3600)


class DeliveryError(Exception):
    """A send that did not go through. `transient` says whether trying again can help."""

    def __init__(
        self,
        message: str,
        *,
        transient: bool,
        status: int | None = None,
        code: str | None = None,
        retry_after: float | None = None,
    ):
        super().__init__(message)
        self.transient = transient
        self.status = status
        self.code = code
        self.retry_after = retry_after
        self.attempts = 0


def is_transient_status(status: int) -> bool:
    return status in TRANSIENT_STATUSES or status >= 500


def parse_retry_after(value: str | None, now: float | None = None) -> float | None:
    """Retry-After is either seconds or an HTTP date."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = parsedate_to_datetime(value).timestamp()
    except (TypeError, ValueError):
        return None
    return max(0.0, when - (time.time() if now is None else now))


def queue_delay(attempts_done: int, retry_after: float | None = None) -> float:
    """Seconds until the next queued try, after `attempts_done` queued tries failed."""
    base = QUEUE_BACKOFF[min(max(attempts_done - 1, 0), len(QUEUE_BACKOFF) - 1)]
    return max(float(base), retry_after or 0.0)


async def deliver_now(
    send: Callable[[], Awaitable[int]],
    *,
    delays: tuple[float, ...] = IN_SESSION_DELAYS,
    budget: float = IN_SESSION_BUDGET,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> tuple[int, int]:
    """Try `send` until it succeeds, fails permanently, or the tries/budget run out.

    Returns (graph status, attempts). Raises the last DeliveryError, with `.attempts` set.
    """
    deadline = clock() + budget
    attempt = 0
    while True:
        attempt += 1
        try:
            return await send(), attempt
        except DeliveryError as error:
            error.attempts = attempt
            if not error.transient or attempt > len(delays):
                raise
            wait = delays[attempt - 1] * random.uniform(0.8, 1.2)  # noqa: S311 - jitter, not crypto
            if error.retry_after is not None:
                wait = max(wait, error.retry_after)
            if clock() + wait >= deadline:
                raise
            await sleep(wait)
