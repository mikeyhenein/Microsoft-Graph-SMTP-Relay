"""End to end: a real SMTP server on a random port, smtplib as the device, a fake Graph."""

import logging
import textwrap
import time

from conftest import StubMsal, exchange, make_message
from mail_queue import MailQueue


def log_lines(caplog, prefix: str) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == "relay" and r.getMessage().startswith(prefix)]


async def test_plaintext_login_sends_and_logs_the_device_ip(relay, fake_graph, caplog):
    caplog.set_level(logging.INFO, logger="relay")
    result = await exchange(relay.port)
    assert result.login[0] == 235
    assert result.data == (250, b"2.0.0 Message accepted for delivery")
    assert len(fake_graph.of("/sendMail")) == 1

    [auth] = log_lines(caplog, "auth ok")
    assert auth.levelno == logging.WARNING
    assert "ip=127.0.0.1" in auth.getMessage() and "tls=no" in auth.getMessage()
    [message] = log_lines(caplog, "message")
    text = message.getMessage()
    for field in ("ip=127.0.0.1", "user=relay", "from=scanner@contoso.com", "to=boss@example.com",
                  "result=sent", "graph=202", "attempts=1", "tls=no"):
        assert field in text
    assert "name=test-client" in log_lines(caplog, "helo")[0].getMessage()


async def test_starttls_login_is_logged_as_encrypted(relay, caplog):
    caplog.set_level(logging.INFO, logger="relay")
    result = await exchange(relay.port, starttls=True)
    assert result.data[0] == 250
    [auth] = log_lines(caplog, "auth ok")
    assert auth.levelno == logging.INFO and "tls=yes" in auth.getMessage()


async def test_mail_without_login_is_refused(relay, fake_graph):
    result = await exchange(relay.port, user=None)
    assert result.mail[0] == 530
    assert fake_graph.requests == []


async def test_wrong_password_then_lockout(relay, caplog):
    caplog.set_level(logging.INFO, logger="relay")
    # smtplib tries PLAIN, then LOGIN: each bad exchange is two failed attempts.
    assert (await exchange(relay.port, password="wrong")).login[0] == 535
    assert (await exchange(relay.port, password="wrong")).login[0] == 535
    assert (await exchange(relay.port, password="wrong")).login[0] in (535, 454)  # 5th failure locks
    locked = await exchange(relay.port)  # right password, but the IP is locked now
    assert locked.login[0] == 454
    assert log_lines(caplog, "auth locked")
    assert "fails=5" in log_lines(caplog, "auth failed")[-1].getMessage()


async def test_sender_rules(relay, fake_graph, caplog):
    caplog.set_level(logging.INFO, logger="relay")
    assert (await exchange(relay.port, sender="ceo@contoso.com")).mail[0] == 550
    assert (await exchange(relay.port, sender="")).mail[0] == 553
    assert (await exchange(relay.port, sender="a/../b@lulu.test")).mail[0] == 553
    mismatch = await exchange(relay.port, sender="scanner@contoso.com",
                              message=make_message(sender="ceo@contoso.com"))
    assert mismatch.data[0] == 550
    assert fake_graph.requests == []
    reasons = [r.getMessage() for r in log_lines(caplog, "reject")]
    assert any("reason=sender-not-allowed" in r and "ip=127.0.0.1" in r for r in reasons)


async def test_wildcard_domain_sender_is_allowed(relay):
    result = await exchange(relay.port, sender="alerts@lulu.test", message=make_message(sender="alerts@lulu.test"))
    assert result.data[0] == 250


async def test_envelope_only_recipients_become_bcc(relay, fake_graph):
    message = make_message()
    result = await exchange(relay.port, rcpts=("boss@example.com", "hidden@example.com"), message=message)
    assert result.data[0] == 250
    sent = fake_graph.sent_mime()
    assert sent == b"Bcc: hidden@example.com\r\n" + message  # the device's message itself is untouched


async def test_transient_failures_are_retried_in_session(relay, fake_graph, caplog):
    caplog.set_level(logging.INFO, logger="relay")
    fake_graph.script("sendMail", 503, (429, 0))
    result = await exchange(relay.port)
    assert result.data[0] == 250
    assert len(fake_graph.of("/sendMail")) == 3
    assert "attempts=3" in log_lines(caplog, "message")[0].getMessage()


async def test_permanent_failure_is_rejected_not_queued(relay, fake_graph, settings):
    fake_graph.script("sendMail", 403)
    result = await exchange(relay.port)
    assert result.data == (550, b"5.7.1 Microsoft 365 refused to send as this mailbox")
    assert relay.queue.jobs() == []


async def test_outage_queues_then_worker_delivers_after_restart(settings, fake_graph, start_relay, caplog):
    caplog.set_level(logging.INFO, logger="relay")
    fake_graph.defaults["sendMail"] = 503
    first = await start_relay(settings)
    result = await exchange(first.port)
    assert result.data[0] == 250 and result.data[1].startswith(b"2.0.0 Queued as ")
    [job] = first.queue.jobs()
    assert job.client_ip == "127.0.0.1" and job.mailbox == "scanner@contoso.com"
    queued_line = log_lines(caplog, "message")[0]
    assert "result=queued" in queued_line.getMessage() and f"queue_id={job.id}" in queued_line.getMessage()
    await first.stop(grace=1)

    # Microsoft recovers; a new relay process picks up the queue from disk.
    fake_graph.defaults.pop("sendMail")
    queue = MailQueue.open(settings.queue_dir, settings.queue_max_age_hours)
    queue._clock = lambda: time.time() + 120  # past the first 60s backoff
    second = await start_relay(settings)
    await queue.process_due(second.handler.deliver_queued)
    assert queue.jobs() == []
    assert len([r for r in fake_graph.of("/sendMail")]) == 4  # 3 in-session tries + 1 from the queue
    assert log_lines(caplog, "queue sent")


async def test_microsoft_unreachable_at_startup_still_accepts_and_queues(settings, fake_graph, start_relay, caplog):
    caplog.set_level(logging.INFO, logger="relay")
    reachable = {"yes": False}

    def build_msal():
        # Like MSAL's constructor when login.microsoftonline.com cannot be reached
        if not reachable["yes"]:
            raise ValueError("Unable to get authority configuration for https://login.microsoftonline.com/x")
        return StubMsal()

    relay = await start_relay(settings, msal_factory=build_msal)
    assert log_lines(caplog, "graph setup-failed")
    result = await exchange(relay.port)
    assert result.data[1].startswith(b"2.0.0 Queued as ")
    assert fake_graph.requests == []

    reachable["yes"] = True
    relay.queue._clock = lambda: time.time() + 120
    await relay.queue.process_due(relay.handler.deliver_queued)
    assert relay.queue.jobs() == []
    assert len(fake_graph.of("/sendMail")) == 1


async def test_without_a_queue_outage_answers_451(settings, fake_graph, start_relay, tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("x")
    relay = await start_relay(settings, queue_dir=blocker / "queue")
    fake_graph.defaults["sendMail"] = 503
    result = await exchange(relay.port)
    assert result.data[0] == 451


async def test_oversized_message_is_refused_by_size(settings, start_relay, fake_graph):
    relay = await start_relay(settings, max_message_size=2048)
    result = await exchange(relay.port, message=make_message(attachment=b"x" * 4096))
    assert result.data[0] == 552
    assert fake_graph.requests == []


def write_middleware(settings, name: str, source: str) -> None:
    settings.middleware_dir.mkdir(parents=True, exist_ok=True)
    (settings.middleware_dir / name).write_text(textwrap.dedent(source))


async def test_middleware_can_skip_sending(settings, start_relay, fake_graph):
    write_middleware(settings, "skip.py", """
        from event_bus import event_bus_instance
        from handlers.microsoft_graph import MicrosoftGraphHandler

        class Middleware:
            def __init__(self, msGraphHandler: MicrosoftGraphHandler):
                assert msGraphHandler.app is not None
                event_bus_instance.subscribe('skip_send', self.skip_sendmail)

            def skip_sendmail(self):
                return True
    """)
    relay = await start_relay(settings)
    result = await exchange(relay.port)
    assert result.data == (250, b"2.0.0 Message accepted (delivery skipped)")
    assert fake_graph.requests == []


async def test_middleware_can_rewrite_subject_and_reply_to(settings, start_relay, fake_graph):
    write_middleware(settings, "rewrite.py", """
        from event_bus import event_bus_instance

        class Middleware:
            def __init__(self, handler):
                event_bus_instance.subscribe('before_send', self.prefix)
                event_bus_instance.subscribe('sender', self.reply_to)

            def prefix(self, message):
                message.replace_header('Subject', '[relay] ' + message['Subject'])

            async def reply_to(self, sender, reply_to):
                reply_to.clear()
                reply_to.append({"emailAddress": {"address": "helpdesk@contoso.com", "name": "Help Desk"}})
    """)
    relay = await start_relay(settings)
    assert (await exchange(relay.port)).data[0] == 250
    sent = fake_graph.sent_mime()
    assert b"Subject: [relay] Scan" in sent
    assert b"Reply-To: Help Desk <helpdesk@contoso.com>" in sent


async def test_failing_middleware_fails_closed(settings, start_relay, fake_graph):
    write_middleware(settings, "broken.py", """
        from event_bus import event_bus_instance

        class Middleware:
            def __init__(self, handler):
                event_bus_instance.subscribe('before_send', self.explode)

            def explode(self, message):
                raise RuntimeError("compliance check unavailable")
    """)
    relay = await start_relay(settings)
    assert (await exchange(relay.port)).data[0] == 451
    assert fake_graph.requests == []


async def test_healthcheck_style_connections_stay_out_of_info_logs(relay, caplog):
    caplog.set_level(logging.INFO, logger="relay")
    await exchange(relay.port)
    assert not [r for r in log_lines(caplog, "connect") if r.levelno >= logging.INFO]
