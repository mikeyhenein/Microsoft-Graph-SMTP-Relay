import asyncio
import contextlib
import json
import logging
import time
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import formataddr

from aiosmtpd.smtp import SMTP, Envelope, Session

import hostnames
from config import Settings
from event_bus import event_bus_instance
from handlers.access import normalize_mailbox, sender_allowed, valid_recipient
from handlers.graph_client import GraphClient, graph_recipients, header_addresses
from logfmt import kv, session_fields
from mail_queue import Job, MailQueue
from retry import DeliveryError, deliver_now, queue_delay

log = logging.getLogger("relay")

MIDDLEWARE_EVENTS = ("before_send", "sender", "recipients")


def _set_address_header(msg: EmailMessage, header: str, recipients: list[dict]) -> None:
    del msg[header]
    if recipients:
        msg[header] = ", ".join(
            formataddr((r["emailAddress"].get("name", ""), r["emailAddress"]["address"])) for r in recipients
        )


def _permanent_reply(error: DeliveryError) -> str:
    if error.status == 403:
        return "550 5.7.1 Microsoft 365 refused to send as this mailbox"
    if error.status == 404:
        return "550 5.1.8 Sending mailbox not found in Microsoft 365"
    if error.status == 413:
        return "552 5.3.4 Message too large"
    return "554 5.6.0 Message rejected by Microsoft 365"


class MicrosoftGraphHandler:
    """
    aiosmtpd handler: checks the sender, runs middleware events, then sends the message
    through Microsoft Graph, retrying briefly and falling back to the disk queue.
    """

    def __init__(self, settings: Settings, graph: GraphClient, queue: MailQueue | None = None, bus=None):
        self.settings = settings
        self.graph = graph
        self.queue = queue
        self.bus = bus or event_bus_instance
        self._in_flight = 0
        self._idle = asyncio.Event()
        self._idle.set()

    @property
    def app(self):
        """The MSAL ConfidentialClientApplication, kept for middleware written against v1."""
        return self.graph.app

    # --- SMTP hooks -------------------------------------------------------------

    async def handle_EHLO(self, server: SMTP, session: Session, envelope: Envelope, hostname: str, responses):
        session.host_name = hostname
        log.info(kv("helo", **session_fields(session), host=await hostnames.wait_for_name(session)))
        return responses

    async def handle_HELO(self, server: SMTP, session: Session, envelope: Envelope, hostname: str):
        session.host_name = hostname
        log.info(kv("helo", **session_fields(session), host=await hostnames.wait_for_name(session)))
        return f"250 {server.hostname}"

    async def handle_MAIL(self, server: SMTP, session: Session, envelope: Envelope, address: str, mail_options):
        mailbox = normalize_mailbox(address)
        if mailbox is None:
            return self._reject(session, "MAIL", "553 5.1.7 Invalid sender address", "invalid-sender", address)
        if not sender_allowed(mailbox, self.settings.allowed_senders):
            return self._reject(session, "MAIL", "550 5.7.1 Sender address not allowed for this login",
                                "sender-not-allowed", address)
        envelope.mail_from = address
        envelope.mail_options.extend(mail_options)
        return "250 OK"

    async def handle_RCPT(self, server: SMTP, session: Session, envelope: Envelope, address: str, rcpt_options):
        if not valid_recipient(address):
            return self._reject(session, "RCPT", "553 5.1.3 Invalid recipient address", "invalid-recipient",
                                address)
        envelope.rcpt_tos.append(address)
        envelope.rcpt_options.extend(rcpt_options)
        return "250 OK"

    async def handle_DATA(self, server: SMTP, session: Session, envelope: Envelope) -> str:
        started = time.monotonic()
        raw = envelope.original_content or b""
        summary = {
            **session_fields(session),
            "from": envelope.mail_from,
            "to": ",".join(envelope.rcpt_tos),
            "size": len(raw),
        }

        def finish(result: str, reply: str, level=logging.INFO, **extra) -> str:
            log.log(level, kv("message", **summary, result=result, **extra,
                              ms=int((time.monotonic() - started) * 1000)))
            return reply

        try:
            msg = BytesParser(policy=policy.default).parsebytes(raw)
        except Exception:
            return finish("rejected", "554 5.6.0 Message could not be parsed", logging.WARNING,
                          reason="unparseable")

        from_header = header_addresses(msg, "From")
        if len(from_header) > 1 or (from_header and from_header[0][1].lower() != envelope.mail_from.lower()):
            return finish("rejected", "550 5.7.1 From header must match the sender address", logging.WARNING,
                          reason="from-header-mismatch", header_from=",".join(a for _, a in from_header))

        try:
            mime = await self._prepare(msg, raw, envelope, summary)
        except Exception:
            log.exception(kv("middleware error", sid=summary["sid"], ip=summary["ip"]))
            return finish("deferred", "451 4.3.0 Message processing failed, try again later", logging.WARNING,
                          reason="middleware-error")
        if mime is None:
            return finish("skipped", "250 2.0.0 Message accepted (delivery skipped)", reason="skip_send")

        mailbox = normalize_mailbox(envelope.mail_from)
        with self._track():
            try:
                status, attempts = await deliver_now(lambda: self.graph.send(mailbox, mime))
            except DeliveryError as error:
                if not error.transient:
                    return finish("rejected", _permanent_reply(error), logging.WARNING, graph=error.status,
                                  attempts=error.attempts, reason=error)
                return await self._queue_or_defer(error, mailbox, mime, envelope, summary, finish)

        await self._after_send(summary)
        return finish("sent", "250 2.0.0 Message accepted for delivery", graph=status, attempts=attempts)

    # --- helpers ----------------------------------------------------------------

    async def _prepare(self, msg: EmailMessage, raw: bytes, envelope: Envelope, summary: dict) -> bytes | None:
        """
        The bytes to hand to Graph, or None when middleware asked to skip the send.

        Without middleware the device's original message goes out untouched; the only
        addition is a Bcc header for envelope recipients that are not in To/Cc, because
        Graph delivers to the addresses in the headers.
        """
        hooks = self.bus.has_subscribers(*MIDDLEWARE_EVENTS)
        if hooks:
            await self.bus.publish("before_send", msg)

        to = graph_recipients(header_addresses(msg, "To"))
        cc = graph_recipients(header_addresses(msg, "Cc"))
        reply_to = graph_recipients(header_addresses(msg, "Reply-To"))
        existing_bcc = header_addresses(msg, "Bcc")
        in_headers = {r["emailAddress"]["address"].lower() for r in to + cc}
        in_headers.update(a.lower() for _, a in existing_bcc)

        envelope_set = {r.lower() for r in envelope.rcpt_tos}
        header_only = sorted(a for a in in_headers if a not in envelope_set)
        if header_only:
            log.warning(kv("header-recipients-not-in-envelope", sid=summary["sid"], ip=summary["ip"],
                           addresses=",".join(header_only)))

        bcc_addresses = list(dict.fromkeys(r for r in envelope.rcpt_tos if r.lower() not in in_headers))
        bcc = graph_recipients([("", a) for a in bcc_addresses])

        changed = False
        if hooks:
            before = json.dumps([to, cc, bcc, reply_to])
            await self.bus.publish("sender", envelope.mail_from, reply_to)
            await self.bus.publish("recipients", to, cc, bcc)
            changed = json.dumps([to, cc, bcc, reply_to]) != before

        if await self.bus.publish("skip_send"):
            return None

        if not hooks:
            if not bcc_addresses:
                return raw
            eol = b"\r\n" if b"\r\n" in raw[:2048] else b"\n"  # match the message's own line endings
            return b"Bcc: " + (b"," + eol + b" ").join(a.encode() for a in bcc_addresses) + eol + raw

        if changed:
            _set_address_header(msg, "To", to)
            _set_address_header(msg, "Cc", cc)
            _set_address_header(msg, "Reply-To", reply_to)
        if changed or bcc:
            _set_address_header(msg, "Bcc", graph_recipients(existing_bcc) + bcc)
        return msg.as_bytes(policy=msg.policy.clone(linesep="\r\n"))

    async def _queue_or_defer(self, error, mailbox, mime, envelope, summary, finish) -> str:
        if self.queue is None:
            return finish("deferred", "451 4.4.0 Microsoft 365 is temporarily unavailable, try again later",
                          logging.WARNING, graph=error.status, attempts=error.attempts, reason=error)
        job = Job(
            id=Job.new_id(),
            mailbox=mailbox,
            rcpt_tos=list(envelope.rcpt_tos),
            client_ip=summary["ip"],
            user=summary["user"],
            sid=summary["sid"],
            size=len(mime),
            next_attempt=time.time() + queue_delay(0, error.retry_after),
            last_error=str(error),
        )
        try:
            await asyncio.to_thread(self.queue.put, job, mime)
        except OSError as write_error:
            log.error(kv("queue write-failed", id=job.id, error=write_error))
            return finish("deferred", "451 4.3.0 Temporary failure, try again later", logging.WARNING,
                          graph=error.status, attempts=error.attempts, reason=error)
        return finish("queued", f"250 2.0.0 Queued as {job.id}", logging.WARNING, graph=error.status,
                      attempts=error.attempts, queue_id=job.id, reason=error)

    async def deliver_queued(self, job: Job, mime: bytes) -> int:
        """Called by the queue worker for each due job."""
        with self._track():
            status = await self.graph.send(job.mailbox, mime)
        await self._after_send({"sid": job.sid, "ip": job.client_ip})
        return status

    async def _after_send(self, summary: dict) -> None:
        try:
            await self.bus.publish("after_send")
        except Exception:
            # The mail is already gone; report the hook failure without failing the send.
            log.exception(kv("middleware error", sid=summary.get("sid"), ip=summary.get("ip"), event="after_send"))

    def _reject(self, session, command: str, reply: str, reason: str, address: str) -> str:
        log.warning(kv("reject", **session_fields(session), cmd=command, code=reply[:3], reason=reason,
                       address=address))
        return reply

    @contextlib.contextmanager
    def _track(self):
        self._in_flight += 1
        self._idle.clear()
        try:
            yield
        finally:
            self._in_flight -= 1
            if self._in_flight == 0:
                self._idle.set()

    async def drain(self, grace: float) -> bool:
        """Wait up to `grace` seconds for in-flight sends. True if everything finished in time."""
        try:
            await asyncio.wait_for(self._idle.wait(), grace)
            return True
        except TimeoutError:
            return False
