"""key=value log lines, easy to grep in `docker logs`."""

from __future__ import annotations

import logging

log = logging.getLogger("relay")


def _format(value: object) -> str:
    if value is None or value == "":
        return "-"
    text = str(value).replace("\r", " ").replace("\n", " ")
    if any(c in text for c in ' "='):
        text = '"' + text.replace('"', '\\"') + '"'
    return text


def kv(event: str, **fields: object) -> str:
    """`kv("auth", ip="10.0.0.5", user="relay")` -> 'auth ip=10.0.0.5 user=relay'."""
    return " ".join([event, *(f"{k}={_format(v)}" for k, v in fields.items())])


def peer_ip(session) -> str:
    peer = getattr(session, "peer", None)
    if isinstance(peer, tuple) and peer:
        return str(peer[0])
    return str(peer) if peer else "-"


def session_fields(session) -> dict[str, object]:
    """The fields every per-connection line starts with."""
    auth_data = getattr(session, "auth_data", None)
    login = getattr(auth_data, "login", None)
    return {
        "sid": getattr(session, "sid", None),
        "ip": peer_ip(session),
        "name": getattr(session, "host_name", None),  # what the device calls itself (EHLO)
        "user": login.decode("utf-8", "replace") if isinstance(login, bytes) else None,
        "tls": "yes" if getattr(session, "ssl", None) is not None else "no",
    }
