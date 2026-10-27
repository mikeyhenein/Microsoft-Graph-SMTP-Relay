"""TLS setup failures become one clear log line and a device status, not a traceback."""

import asyncio
import logging
import smtplib
import socket
import ssl

import pytest

import hostnames
from activity import ActivityStore, parse_kv
from smtp_server import classify_tls_failure


def verifying_client(port: int) -> str:
    """Like Invoice Ninja with STARTTLS + Verify Peer: rejects a self-signed certificate."""
    with smtplib.SMTP("127.0.0.1", port, timeout=10) as client:
        client.ehlo("invoiceninja")
        try:
            client.starttls(context=ssl.create_default_context())
        except ssl.SSLCertVerificationError as error:
            return error.verify_message
    return "no error"


def implicit_tls_client(port: int) -> None:
    """Like a device set to SSL/TLS on a STARTTLS port: speaks TLS before any SMTP."""
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    with socket.create_connection(("127.0.0.1", port), timeout=10) as raw:
        try:
            with context.wrap_socket(raw, server_hostname="relay"):
                pass
        except (ssl.SSLError, OSError):
            pass


def relay_lines(caplog, prefix):
    return [r for r in caplog.records if r.name == "relay" and r.getMessage().startswith(prefix)]


async def test_untrusted_certificate_is_one_clear_warning(relay, caplog):
    caplog.set_level(logging.INFO)
    assert await asyncio.to_thread(verifying_client, relay.port) == "self-signed certificate"
    await asyncio.sleep(0.1)
    [line] = relay_lines(caplog, "tls failed")
    event, fields = parse_kv(line.getMessage())
    assert line.levelno == logging.WARNING
    assert fields["reason"] == "untrusted-certificate" and fields["name"] == "invoiceninja"
    assert fields["ip"] == "127.0.0.1" and fields["detail"] == "TLSV1_ALERT_UNKNOWN_CA"
    # No raw traceback from the SMTP library any more
    assert not [r for r in caplog.records if r.name == "mail.log" and r.levelno >= logging.ERROR]


async def test_implicit_tls_is_recognised(relay, caplog):
    caplog.set_level(logging.INFO)
    await asyncio.to_thread(implicit_tls_client, relay.port)
    await asyncio.sleep(0.1)
    [line] = relay_lines(caplog, "tls failed")
    assert parse_kv(line.getMessage())[1]["reason"] == "implicit-tls"


@pytest.mark.parametrize("error, reason", [
    (ssl.SSLError(1, "[SSL: TLSV1_ALERT_UNKNOWN_CA] tlsv1 alert unknown ca"), "handshake-failed"),
    (ssl.SSLEOFError(8, "EOF"), "aborted"),
    (ConnectionResetError(), "aborted"),
    (None, "handshake-failed"),
])
def test_classifier_fallbacks(error, reason):
    # Hand-built SSLErrors carry no .reason; real ones do (covered by the end-to-end test above).
    assert classify_tls_failure(error)[0] == reason


def test_device_shows_the_tls_problem_until_it_works_again(tmp_path):
    clock = [1_800_000_000.0]
    store = ActivityStore(tmp_path / "a.db", clock=lambda: clock[0])
    store.record(*parse_kv("helo sid=a ip=172.18.0.1 user=- tls=no name=invoiceninja host=ninja.lan"), clock[0])
    store.record(*parse_kv("tls failed sid=a ip=172.18.0.1 name=invoiceninja reason=untrusted-certificate detail=X"),
                 clock[0])
    [device] = store.summary()["devices"]
    assert device["name"] == "invoiceninja" and device["hostname"] == "ninja.lan"
    assert device["last_reason"] == "tls-untrusted-certificate"
    assert device["last_result"] == "TLS failed: certificate not trusted" and device["last_kind"] == "rejected"

    store.record(*parse_kv("auth ok sid=b ip=172.18.0.1 user=relay tls=yes mech=PLAIN"), clock[0] + 5)
    [device] = store.summary()["devices"]
    assert device["last_reason"] is None and device["last_result"] == "Logged in"


async def test_hostname_lookup_skips_loopback_and_caches(monkeypatch):
    hostnames._cache.clear()
    assert await hostnames.lookup("127.0.0.1") is None
    assert await hostnames.lookup("not-an-ip") is None
    calls = []

    async def fake_getnameinfo(self, sockaddr, flags=0):
        calls.append(sockaddr)
        return ("scanner.lan", "0")

    monkeypatch.setattr(asyncio.get_running_loop().__class__, "getnameinfo", fake_getnameinfo)
    assert await hostnames.lookup("192.168.26.40") == "scanner.lan"
    assert await hostnames.lookup("192.168.26.40") == "scanner.lan"
    assert len(calls) == 1
