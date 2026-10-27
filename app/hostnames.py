"""Reverse-DNS names for client IPs: cached, and never holding up an SMTP session for long."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import time

TTL = 3600.0
LOOKUP_TIMEOUT = 2.0

_cache: dict[str, tuple[str | None, float]] = {}


async def lookup(ip: str) -> str | None:
    """The PTR name for `ip` (e.g. from your router's DNS, or Docker's for containers), or None."""
    try:
        if ipaddress.ip_address(ip).is_loopback:
            return None
    except ValueError:
        return None
    now = time.monotonic()
    cached = _cache.get(ip)
    if cached and cached[1] > now:
        return cached[0]
    name = None
    try:
        host, _ = await asyncio.wait_for(
            asyncio.get_running_loop().getnameinfo((ip, 0), socket.NI_NAMEREQD), LOOKUP_TIMEOUT
        )
        name = host if host and host != ip else None
    except (OSError, TimeoutError):
        name = None
    _cache[ip] = (name, now + TTL)
    return name


async def wait_for_name(session, wait: float = 1.5) -> str | None:
    """The name being looked up for this session's client, if it arrives within `wait` seconds."""
    task = getattr(session, "hostname_task", None)
    if task is None:
        return None
    try:
        return await asyncio.wait_for(asyncio.shield(task), wait)
    except Exception:
        return None


def known_name(session) -> str | None:
    """The looked-up name if it is already available; never waits."""
    task = getattr(session, "hostname_task", None)
    if task is None or not task.done() or task.cancelled() or task.exception() is not None:
        return None
    return task.result()
