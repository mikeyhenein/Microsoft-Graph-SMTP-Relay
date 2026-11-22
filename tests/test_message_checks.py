"""Header checks and parsing limits (red-team findings 3, 4 and 8)."""

import asyncio
import logging
import time

import pytest

from conftest import exchange, make_message
from handlers.headers import HeaderProblem, check_headers

OK = b"From: scanner@contoso.com\r\nTo: boss@example.com\r\nSubject: s\r\n"


def message(headers: bytes, body: bytes = b"body\r\n") -> bytes:
    return headers + b"\r\n" + body


def message_lines(caplog):
    return [r.msg.fields for r in caplog.records if r.name == "relay" and getattr(r.msg, "event", "") == "message"]


@pytest.mark.parametrize("headers, code, reason", [
    (b"From: ceo@contoso.com\r\nTo: boss@example.com\r\n", 550, "from-header-mismatch"),
    (OK + b"From: ceo@contoso.com\r\n", 550, "from-header-mismatch"),
    (b"From: ceo@contoso.com,\r\nTo: boss@example.com\r\n", 550, "from-header-mismatch"),
    (b"From: undisclosed\r\nTo: boss@example.com\r\n", 550, "from-header-mismatch"),
    (b"From : ceo@contoso.com\r\nTo: boss@example.com\r\n", 554, "malformed-headers"),
    (b"X-A : b\r\nFrom: ceo@contoso.com\r\nTo: boss@example.com\r\n", 554, "malformed-headers"),
    (b" folded-first\r\n" + OK, 554, "malformed-headers"),
    (b"From: scanner@contoso.com\r\nTo: boss@example.com\nFrom: ceo@contoso.com\r\n", 554, "malformed-headers"),
    (b"From: scanner@contoso.com\r\nTo: boss\rFrom: ceo@contoso.com\r\n", 554, "malformed-headers"),
    (b"From: scanner@contoso.com\x00\r\nTo: boss@example.com\r\n", 554, "malformed-headers"),
    (OK + b"Sender: ceo@contoso.com\r\n", 550, "sender-header-mismatch"),
])
async def test_from_and_sender_checks_fail_closed(relay, fake_graph, caplog, headers, code, reason):
    caplog.set_level(logging.INFO, logger="relay")
    result = await exchange(relay.port, message=message(headers))
    assert result.data[0] == code
    assert message_lines(caplog)[-1]["reason"] == reason
    assert fake_graph.requests == []


@pytest.mark.parametrize("headers", [
    OK,
    OK + b"Sender: Scanner <SCANNER@contoso.com>\r\n",
    b"To: boss@example.com\r\nSubject: no From line, Microsoft fills it in\r\n",
    b'From: "Scanner, 2nd floor" <scanner@contoso.com>\r\nTo: boss@example.com\r\n',
])
async def test_well_formed_messages_still_go_through(relay, fake_graph, headers):
    assert (await exchange(relay.port, message=message(headers))).data[0] == 250


async def test_lf_only_messages_still_go_through(relay, fake_graph):
    result = await exchange(relay.port, message=make_message().replace(b"\r\n", b"\n"))
    assert result.data[0] == 250


async def test_huge_address_header_is_refused_at_once(relay, fake_graph, caplog):
    caplog.set_level(logging.INFO, logger="relay")
    to = b"To: " + b",\r\n ".join([b"a@b.test"] * 40_000) + b"\r\n"  # ~360 KB, folded into normal-length lines
    started = time.monotonic()
    result = await exchange(relay.port, message=message(b"From: scanner@contoso.com\r\n" + to))
    assert result.data[0] == 552
    assert time.monotonic() - started < 2
    assert message_lines(caplog)[-1]["reason"] == "headers-too-large"


async def test_a_slow_message_does_not_hold_up_other_devices(relay, fake_graph):
    addresses = [f"a{i}@b.test".encode() for i in range(3_500)]
    to = b"To: " + b",\r\n ".join(addresses) + b"\r\n"  # ~56 KB, just under the 64 KB header cap
    slow = asyncio.create_task(exchange(relay.port, message=message(b"From: scanner@contoso.com\r\n" + to)))
    await asyncio.sleep(0.05)
    started = time.monotonic()
    assert (await exchange(relay.port)).data[0] == 250
    assert time.monotonic() - started < 2
    assert (await slow).data[0] == 550  # 3,500 recipients is over the 500 limit


async def test_too_many_recipients(relay, fake_graph):
    result = await exchange(relay.port, rcpts=[f"r{i}@example.com" for i in range(501)])
    assert result.rcpt[499][0] == 250 and result.rcpt[500][0] == 452
    assert result.data[0] == 250  # the first 500 still go


async def test_deeply_nested_header_gets_a_plain_permanent_error(relay, fake_graph):
    nested = b"From: " + b"\r\n ".join([b"(" * 900, b"(" * 900, b"(" * 200]) + b"scanner@contoso.com\r\n"
    result = await exchange(relay.port, message=message(nested + b"To: boss@example.com\r\n"))
    assert result.data[0] in (550, 554)
    assert b"Error" not in result.data[1] and b"Recursion" not in result.data[1]


async def test_header_only_recipients_are_listed_on_the_message(relay, fake_graph, caplog):
    caplog.set_level(logging.INFO, logger="relay")
    headers = OK + b"Cc: also@example.com\r\n"
    assert (await exchange(relay.port, message=message(headers))).data[0] == 250
    assert message_lines(caplog)[-1]["to"] == "boss@example.com,also@example.com"


def test_check_headers_counts_names():
    assert check_headers(message(OK + b"Received: a\r\n\tb\r\n")) == {"from": 1, "to": 1, "subject": 1, "received": 1}
    with pytest.raises(HeaderProblem):
        check_headers(b"From " + OK)  # mbox separator line
