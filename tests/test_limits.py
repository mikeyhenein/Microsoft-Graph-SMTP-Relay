"""Limits that keep one client from starving the others (red-team findings 1 and 7)."""

import asyncio
import socket

import smtp_server
from conftest import exchange
from handlers.access import ConnectionGuard


def raw_session(port: int, payload: bytes, wait: float = 3.0) -> bytes:
    """Send `payload` right after the greeting and return everything the relay answered until it hung up."""
    with socket.create_connection(("127.0.0.1", port), timeout=wait) as s:
        s.recv(1024)  # 220 greeting
        s.sendall(payload)
        received = b""
        try:
            while chunk := s.recv(65536):
                received += chunk
        except (TimeoutError, ConnectionResetError):
            pass
        return received


def relay_lines(caplog, prefix):
    return [r for r in caplog.records if r.name == "relay" and r.getMessage().startswith(prefix)]


async def test_repeated_ehlo_is_cut_off(relay, caplog):
    reply = await asyncio.to_thread(raw_session, relay.port, b"EHLO flood\r\n" * 15)
    assert reply.count(b"250-") >= 10
    assert b"421 4.7.0 EHLO sent too many times" in reply
    [line] = relay_lines(caplog, "connection dropped")
    assert line.msg.fields["reason"] == "too-many-commands" and line.msg.fields["cmd"] == "EHLO"
    assert len(relay_lines(caplog, "helo")) == 10


async def test_too_much_data_before_login_is_dropped(relay, caplog):
    reply = await asyncio.to_thread(raw_session, relay.port, b"EHLO x\r\n" + b"X" * 5000)
    assert reply.endswith(b"421 4.7.0 Too much data before login\r\n")
    [line] = relay_lines(caplog, "connection dropped")
    assert line.msg.fields["reason"] == "too-much-data-before-login"


async def test_large_messages_after_login_are_unaffected(relay, fake_graph):
    from conftest import make_message
    result = await exchange(relay.port, message=make_message(attachment=b"%PDF" + b"x" * 200_000))
    assert result.data[0] == 250


async def test_too_many_connections_from_one_address_are_turned_away(relay, monkeypatch, caplog):
    monkeypatch.setattr(smtp_server, "_is_loopback", lambda ip: False)  # treat the test client like a LAN device
    relay.connection_guard.per_ip = 3

    def open_and_greet():
        s = socket.create_connection(("127.0.0.1", relay.port), timeout=5)
        return s, s.recv(1024)

    held = []
    try:
        for _ in range(3):
            s, greeting = await asyncio.to_thread(open_and_greet)
            held.append(s)
            assert greeting.startswith(b"220 ")
        for _ in range(2):
            s, greeting = await asyncio.to_thread(open_and_greet)
            s.close()
            assert greeting == b"421 4.7.0 Too many connections, try again later\r\n"
        await asyncio.sleep(0.1)
        [line] = relay_lines(caplog, "connection refused")  # one line per address per minute
        assert line.msg.fields["reason"] == "too-many-open-connections"
    finally:
        for s in held:
            s.close()
    await asyncio.sleep(0.2)
    assert relay.connection_guard.open_total == 0
    result = await exchange(relay.port)
    assert result.data[0] == 250


def test_connection_guard_counts():
    now = [0.0]
    guard = ConnectionGuard(per_ip=2, per_minute=4, total=4, clock=lambda: now[0])
    assert guard.admit("a") is None and guard.admit("a") is None
    assert guard.admit("a") == "too-many-open-connections"
    guard.release("a")
    assert guard.admit("a") is None
    guard.release("a")
    guard.release("a")
    assert guard.admit("a") == "too-many-new-connections"  # 5th new connection this minute
    now[0] += 60
    assert guard.admit("a") is None
    assert guard.admit("b") is None and guard.admit("b") is None and guard.admit("c") is None
    assert guard.admit("d") == "too-many-connections-in-total"
    assert guard.refused_to_log("d") == 1
    assert guard.refused_to_log("d") is None
    now[0] += 61
    assert guard.refused_to_log("d") == 2
