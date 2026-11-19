"""
A strict, linear-time look at a message's raw header block before anything parses it.

Python's email parser is lenient: it reads `From : x` (space before the colon) or a
malformed first line in ways Microsoft 365 may not, and its address parser slows down
with the square of a header's size. Refusing what is not plainly well formed keeps the
relay's From check and Microsoft's reading of the message the same, and keeps parsing fast.
"""

from __future__ import annotations

import re

MAX_HEADER_BYTES = 64 * 1024
# RFC 5322 field name: printable ASCII except the colon, followed directly by the colon.
_FIELD = re.compile(rb"^([\x21-\x39\x3b-\x7e]+):")


class HeaderProblem(Exception):
    """Why a header block was refused; `reason` is the log/dashboard code."""

    def __init__(self, reason: str, reply: str):
        super().__init__(reason)
        self.reason = reason
        self.reply = reply


def header_block(raw: bytes) -> tuple[bytes, bytes]:
    """(header block, line ending). A message with no blank line is all headers."""
    crlf = raw.find(b"\r\n\r\n")
    lf = raw.find(b"\n\n")
    if crlf != -1 and (lf == -1 or crlf < lf):
        return raw[:crlf], b"\r\n"
    if lf != -1:
        return raw[:lf], b"\n"
    return raw, b"\r\n" if b"\r\n" in raw else b"\n"


def check_headers(raw: bytes) -> dict[str, int]:
    """
    Raise HeaderProblem unless every header line is `Name:` or a continuation line.
    Returns how many times each header name (lower case) appears.
    """
    block, eol = header_block(raw)
    if len(block) > MAX_HEADER_BYTES:
        raise HeaderProblem("headers-too-large", "552 5.3.4 Message headers too large")
    if b"\x00" in block:
        raise HeaderProblem("malformed-headers", "554 5.6.0 Malformed message headers")
    counts: dict[str, int] = {}
    for number, line in enumerate(block.split(eol)):
        if b"\r" in line or (eol == b"\r\n" and b"\n" in line):
            raise HeaderProblem("malformed-headers", "554 5.6.0 Malformed message headers")
        if line[:1] in (b" ", b"\t"):
            if number == 0:
                raise HeaderProblem("malformed-headers", "554 5.6.0 Malformed message headers")
            continue
        match = _FIELD.match(line)
        if match is None:
            raise HeaderProblem("malformed-headers", "554 5.6.0 Malformed message headers")
        name = match.group(1).decode("ascii").lower()
        counts[name] = counts.get(name, 0) + 1
    return counts
