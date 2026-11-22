"""key=value log lines, easy to grep in `docker logs`."""

from __future__ import annotations

import logging
import re

log = logging.getLogger("relay")

# Longest value written to a log line. Device-supplied names are capped much lower in session_fields.
MAX_VALUE = 20_000
MAX_NAME = 100

# Line breaks and tabs become spaces; other control characters (terminal escape codes, NUL, bidi
# overrides, zero-width characters, lone surrogates from undecodable bytes) become "?".
_SPACE = re.compile(r"[\r\n\t]")
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f​-‏‪-‮⁦-⁩﻿\ud800-\udfff]")


def clean(value: object, limit: int = MAX_VALUE) -> str:
    """The value as a single printable line of at most `limit` characters."""
    text = _CONTROL.sub("?", _SPACE.sub(" ", str(value)))
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _format(value: object) -> str:
    if value is None or value == "":
        return "-"
    text = clean(value)
    if any(c in text for c in ' "=\\'):
        text = '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return text


class KV(str):
    """
    A log line that also carries its event name and field values, so the dashboard reads
    the fields directly instead of parsing the text (which a device could forge).
    """

    event: str
    fields: dict[str, str | None]


def kv(event: str, **fields: object) -> KV:
    """`kv("auth", ip="10.0.0.5", user="relay")` -> 'auth ip=10.0.0.5 user=relay'."""
    line = KV(" ".join([event, *(f"{k}={_format(v)}" for k, v in fields.items())]))
    line.event = event
    line.fields = {k: None if v is None or v in ("", "-") else clean(v) for k, v in fields.items()}
    return line


def peer_ip(session) -> str:
    peer = getattr(session, "peer", None)
    if isinstance(peer, tuple) and peer:
        return str(peer[0])
    return str(peer) if peer else "-"


def session_fields(session) -> dict[str, object]:
    """The fields every per-connection line starts with."""
    auth_data = getattr(session, "auth_data", None)
    login = getattr(auth_data, "login", None)
    name = getattr(session, "host_name", None)  # what the device calls itself (EHLO)
    return {
        "sid": getattr(session, "sid", None),
        "ip": peer_ip(session),
        "name": clean(name, MAX_NAME) if name else None,
        "user": clean(login.decode("utf-8", "replace"), MAX_NAME) if isinstance(login, bytes) else None,
        "tls": "yes" if getattr(session, "ssl", None) is not None else "no",
    }
