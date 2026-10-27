"""Who may log in, and which addresses a login may send as."""

from __future__ import annotations

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


class LoginLockout:
    """Block an IP for a while after too many failed logins (brute-force protection)."""

    def __init__(
        self,
        max_failures: int = 5,
        window: float = 900.0,
        lockout: float = 900.0,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.max_failures = max_failures
        self.window = window
        self.lockout = lockout
        self._clock = clock
        self._failures: dict[str, list[float]] = {}
        self._locked_until: dict[str, float] = {}

    def is_locked(self, ip: str) -> bool:
        until = self._locked_until.get(ip)
        if until is None:
            return False
        if self._clock() >= until:
            del self._locked_until[ip]
            self._failures.pop(ip, None)
            return False
        return True

    def record_failure(self, ip: str) -> int:
        """Returns the failure count in the current window."""
        now = self._clock()
        recent = [t for t in self._failures.get(ip, []) if now - t < self.window]
        recent.append(now)
        self._failures[ip] = recent
        if len(recent) >= self.max_failures:
            self._locked_until[ip] = now + self.lockout
        return len(recent)

    def record_success(self, ip: str) -> None:
        self._failures.pop(ip, None)
