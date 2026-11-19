"""Who may log in, and which addresses a login may send as."""

from __future__ import annotations

import collections
import re
import time
from collections.abc import Callable, Iterable

# Mailbox addresses that go into Graph URLs. Deliberately stricter than RFC 5321:
# no '/', '%', '?', '#' or '\' can reach the URL path.
_MAILBOX = re.compile(r"^[A-Za-z0-9_+'-]+(\.[A-Za-z0-9_+'-]+)*@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$")
# Recipients only need to be a plausible single address; Graph does the rest.
_RECIPIENT = re.compile(r"^[^\s@<>,;]+@[^\s@<>,;]+$")


def normalize_mailbox(address: str | None) -> str | None:
    """The sender address, lower-cased, if it is safe to use as a Graph mailbox id."""
    if not address or len(address) > 254:
        return None
    address = address.strip()
    return address.lower() if _MAILBOX.match(address) else None


def valid_recipient(address: str | None) -> bool:
    return bool(address) and len(address) <= 254 and bool(_RECIPIENT.match(address))


def sender_allowed(address: str, patterns: Iterable[str]) -> bool:
    """`patterns` are lower-case `user@domain` or `*@domain` entries."""
    address = address.lower()
    domain = address.rpartition("@")[2]
    for pattern in patterns:
        if pattern.startswith("*@"):
            if domain == pattern[2:]:
                return True
        elif address == pattern:
            return True
    return False


class ConnectionGuard:
    """
    Caps SMTP connections per client address and in total, so one address cannot tie up
    the relay. It limits how much, not who: every address gets the same allowance.
    """

    # per_minute: 10 new connections a second from one address. A batch sender that opens a connection per
    # message (and every app on the Docker host, which share the gateway address) stays well under it.
    def __init__(self, per_ip: int = 50, per_minute: int = 600, total: int = 1000,
                 clock: Callable[[], float] = time.monotonic, max_tracked: int = 10_000):
        self.per_ip = per_ip
        self.per_minute = per_minute
        self.total = total
        self.max_tracked = max_tracked
        self._clock = clock
        self.open: dict[str, int] = {}
        self.open_total = 0
        self._recent: dict[str, tuple[float, int]] = {}  # ip -> (minute window start, new connections)
        self._refused: dict[str, tuple[float, int]] = {}  # ip -> (last logged, refused since then)

    def admit(self, ip: str) -> str | None:
        """None if the connection may go ahead (call release() when it ends), else why it is refused."""
        now = self._clock()
        start, count = self._recent.get(ip, (now, 0))
        if now - start >= 60:
            start, count = now, 0
        if ip not in self._recent and len(self._recent) >= self.max_tracked:
            self._recent = {k: v for k, v in self._recent.items() if now - v[0] < 60}
            if len(self._recent) >= self.max_tracked:
                self._recent.clear()
        self._recent[ip] = (start, count + 1)
        if self.open_total >= self.total:
            return "too-many-connections-in-total"
        if self.open.get(ip, 0) >= self.per_ip:
            return "too-many-open-connections"
        if count + 1 > self.per_minute:
            return "too-many-new-connections"
        self.open[ip] = self.open.get(ip, 0) + 1
        self.open_total += 1
        return None

    def release(self, ip: str) -> None:
        left = self.open.get(ip, 0) - 1
        if left > 0:
            self.open[ip] = left
        else:
            self.open.pop(ip, None)
        self.open_total = max(0, self.open_total - 1)

    def refused_to_log(self, ip: str) -> int | None:
        """Refusals from `ip` to report now (at most one log line per address per minute), or None."""
        now = self._clock()
        last, count = self._refused.get(ip, (None, 0))
        count += 1
        if last is not None and now - last < 60:
            self._refused[ip] = (last, count)
            return None
        if ip not in self._refused and len(self._refused) >= self.max_tracked:
            self._refused.clear()
        self._refused[ip] = (now, 0)
        return count


class LoginLockout:
    """
    Brute-force protection that slows guessing down without locking out the right password.

    After `max_failures` wrong passwords from an address within `window` seconds, that address
    gets one login checked every `slow` seconds; the rest are refused unchecked. The right
    password still gets in on a checked attempt, so devices sharing an address (the Docker
    gateway, a reverse proxy) are delayed, never shut out. When more than `storm` wrong
    passwords a minute arrive across all addresses, every address that has never logged in
    successfully is slowed the same way, so spreading guesses over many addresses does not help.
    """

    def __init__(
        self,
        max_failures: int = 5,
        window: float = 900.0,
        slow: float = 10.0,
        storm: int = 30,
        clock: Callable[[], float] = time.monotonic,
        max_tracked: int = 10_000,
    ):
        self.max_failures = max_failures
        self.window = window
        self.slow = slow
        self.storm = storm
        self.max_tracked = max_tracked
        self._clock = clock
        self._failures: dict[str, list[float]] = {}
        self._last_try: dict[str, float] = {}
        self._all_failures: collections.deque[float] = collections.deque()
        self._trusted: set[str] = set()  # addresses that have logged in successfully

    def _storming(self, now: float) -> bool:
        while self._all_failures and now - self._all_failures[0] >= 60:
            self._all_failures.popleft()
        return len(self._all_failures) > self.storm

    def throttled(self, ip: str) -> bool:
        now = self._clock()
        recent = [t for t in self._failures.get(ip, []) if now - t < self.window]
        if len(recent) >= self.max_failures:
            return True
        return ip not in self._trusted and self._storming(now)

    def allow(self, ip: str) -> bool:
        """False when this attempt must be refused without checking the password."""
        if not self.throttled(ip):
            return True
        now = self._clock()
        last = self._last_try.get(ip)
        if last is not None and now - last < self.slow:
            return False
        if ip not in self._last_try and len(self._last_try) >= self.max_tracked:
            self._last_try.clear()
        self._last_try[ip] = now
        return True

    def record_failure(self, ip: str) -> int:
        """Returns the failure count in the current window."""
        now = self._clock()
        if ip not in self._failures and len(self._failures) >= self.max_tracked:
            self._failures = {k: v for k, v in self._failures.items() if v and now - v[-1] < self.window}
            if len(self._failures) >= self.max_tracked:
                self._failures.clear()
        recent = [t for t in self._failures.get(ip, []) if now - t < self.window]
        recent.append(now)
        self._failures[ip] = recent[-self.max_failures * 4:]
        self._all_failures.append(now)
        if len(self._all_failures) > self.storm * 4:
            self._all_failures.popleft()
        return len(recent)

    def record_success(self, ip: str) -> None:
        self._failures.pop(ip, None)
        self._last_try.pop(ip, None)
        if len(self._trusted) >= self.max_tracked:
            self._trusted.clear()
        self._trusted.add(ip)
