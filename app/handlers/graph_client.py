"""Talks to Microsoft Graph: tokens, sendMail with raw MIME, and the large-message draft path."""

from __future__ import annotations

import asyncio
import base64
import logging
import mimetypes
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import getaddresses
from typing import Any
from urllib.parse import quote

import aiohttp

from config import GRAPH_BASE_URL, GRAPH_SCOPE, SMALL_MESSAGE_LIMIT
from logfmt import kv
from retry import DeliveryError, is_transient_status, parse_retry_after

log = logging.getLogger("relay")

# Upload sessions take attachments of 3 MB to 150 MB; smaller ones are posted directly.
UPLOAD_SESSION_MIN = 3 * 1024 * 1024
# Upload chunks may be up to 4 MB each and must go in order.
UPLOAD_CHUNK = 3 * 1024 * 1024
# Graph allows 4 concurrent requests per app per mailbox.
MAILBOX_CONCURRENCY = 4

IMMUTABLE_ID = 'IdType="ImmutableId"'


@dataclass
class Attachment:
    name: str
    content_type: str
    data: bytes
    is_inline: bool
    content_id: str | None

    @property
    def size(self) -> int:
        return len(self.data)


def header_addresses(msg: EmailMessage, header: str) -> list[tuple[str, str]]:
    """(display name, address) pairs from a header; handles commas inside quoted names."""
    values = [str(v) for v in msg.get_all(header, [])]
    return [(name, addr) for name, addr in getaddresses(values) if addr]


def graph_recipients(pairs: list[tuple[str, str]]) -> list[dict]:
    recipients = []
    for name, addr in pairs:
        entry: dict[str, Any] = {"emailAddress": {"address": addr}}
        if name:
            entry["emailAddress"]["name"] = name
        recipients.append(entry)
    return recipients


def _leaves(part: EmailMessage) -> Iterator[EmailMessage]:
    """Every non-multipart part, without descending into attached messages."""
    if part.get_content_maintype() == "multipart":
        for sub in part.iter_parts():
            yield from _leaves(sub)
    else:
        yield part


def _part_bytes(part: EmailMessage) -> bytes | None:
    if part.get_content_maintype() == "message":
        inner = part.get_content()
        return inner.as_bytes() if isinstance(inner, EmailMessage) else None
    return part.get_payload(decode=True)


def _text_content(part: EmailMessage) -> str:
    try:
        return part.get_content()
    except (LookupError, UnicodeError):
        return (part.get_payload(decode=True) or b"").decode("utf-8", "replace")


def extract_body_and_attachments(msg: EmailMessage) -> tuple[str, str, list[Attachment]]:
    """
    The message body ("html" preferred over "text") and every attachment, inline images included.
    """
    body = msg.get_body(preferencelist=("html", "plain"))
    if body is None:
        body_content, content_type = "", "text"
    else:
        body_content = _text_content(body)
        content_type = "html" if body.get_content_type() == "text/html" else "text"

    attachments = []
    for part in _leaves(msg):
        if part is body:
            continue
        disposition = part.get_content_disposition()
        part_type = part.get_content_type()
        if part_type in ("text/plain", "text/html") and disposition != "attachment" and not part.get_filename():
            continue  # the other alternative of the body
        data = _part_bytes(part)
        if data is None:
            continue
        content_id = (part.get("Content-ID") or "").strip().strip("<>") or None
        name = part.get_filename()
        if not name:
            if part_type == "message/rfc822":
                name = "attached-message.eml"
            else:
                name = f"attachment-{uuid.uuid4().hex[:8]}{mimetypes.guess_extension(part_type) or ''}"
        attachments.append(
            Attachment(
                name=name,
                content_type=part_type,
                data=data,
                is_inline=disposition == "inline" or (disposition is None and content_id is not None),
                content_id=content_id,
            )
        )
    return body_content, content_type, attachments


class GraphClient:
    """One HTTP session for the life of the relay. Raises DeliveryError on any failed call."""

    def __init__(
        self,
        msal_app=None,
        *,
        msal_factory: Callable[[], Any] | None = None,
        base_url: str = GRAPH_BASE_URL,
        large_messages: bool = False,
        small_limit: int = SMALL_MESSAGE_LIMIT,
        timeout: aiohttp.ClientTimeout | None = None,
    ):
        # MSAL contacts Microsoft while it is constructed (tenant discovery), so it is built
        # lazily in a worker thread: an outage at startup queues mail instead of crashing.
        self.app = msal_app
        self._msal_factory = msal_factory
        self._msal_lock = threading.Lock()
        self.base_url = base_url.rstrip("/")
        self.large_messages = large_messages
        self.small_limit = small_limit
        self._timeout = timeout or aiohttp.ClientTimeout(total=120, connect=10)
        self._session: aiohttp.ClientSession | None = None
        self._limits: dict[str, asyncio.Semaphore] = {}
        # Last Microsoft sign-in outcome, for the dashboard: ok is None until the first attempt.
        self.auth_status: dict[str, Any] = {"ok": None, "at": None, "error": None}

    def record_auth(self, ok: bool, error: str | None = None) -> None:
        self.auth_status = {"ok": ok, "at": time.time(), "error": error}

    async def __aenter__(self) -> GraphClient:
        self._session = aiohttp.ClientSession(timeout=self._timeout)
        return self

    async def __aexit__(self, *exc) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def send(self, mailbox: str, mime: bytes) -> int:
        """Send a complete RFC 5322 message as `mailbox`. Returns the Graph status (202)."""
        if len(mime) <= self.small_limit:
            return await self.send_mime(mailbox, mime)
        if self.large_messages:
            return await self.send_large(mailbox, mime)
        raise DeliveryError(
            f"message is {len(mime)} bytes; over {self.small_limit} needs LARGE_MESSAGES=true",
            transient=False,
            status=413,
        )

    async def send_mime(self, mailbox: str, mime: bytes) -> int:
        """sendMail with the raw message: Mail.Send only, message kept exactly as built."""
        headers = await self._auth_headers()
        headers["Content-Type"] = "text/plain"
        status, _ = await self._call(
            "POST", self._url(mailbox, "sendMail"), mailbox, headers=headers,
            data=base64.b64encode(mime), ok=(202,),
        )
        return status

    async def send_large(self, mailbox: str, mime: bytes) -> int:
        """Draft, attachments (posted or uploaded in chunks), then send. Needs Mail.ReadWrite."""
        msg = BytesParser(policy=policy.default).parsebytes(mime)
        body, content_type, attachments = extract_body_and_attachments(msg)
        draft = {
            "subject": str(msg.get("Subject", "")),
            "body": {"contentType": content_type, "content": body},
            "toRecipients": graph_recipients(header_addresses(msg, "To")),
            "ccRecipients": graph_recipients(header_addresses(msg, "Cc")),
            "bccRecipients": graph_recipients(header_addresses(msg, "Bcc")),
        }
        reply_to = graph_recipients(header_addresses(msg, "Reply-To"))
        if reply_to:
            draft["replyTo"] = reply_to

        headers = await self._auth_headers()
        headers["Prefer"] = IMMUTABLE_ID
        _, created = await self._call("POST", self._url(mailbox, "messages"), mailbox,
                                      headers=headers, json=draft, ok=(201,))
        message_id = created["id"]
        try:
            for attachment in attachments:
                await self._add_attachment(mailbox, message_id, attachment, headers)
            status, _ = await self._call("POST", self._url(mailbox, "messages", message_id, "send"),
                                         mailbox, headers=headers, ok=(202,))
            return status
        except DeliveryError:
            await self._discard_draft(mailbox, message_id, headers)
            raise

    async def _add_attachment(self, mailbox: str, message_id: str, att: Attachment, headers: dict) -> None:
        if att.size < UPLOAD_SESSION_MIN:
            payload: dict[str, Any] = {
                "@odata.type": "#microsoft.graph.fileAttachment",
                "name": att.name,
                "contentType": att.content_type,
                "contentBytes": base64.b64encode(att.data).decode("ascii"),
                "isInline": att.is_inline,
            }
            if att.content_id:
                payload["contentId"] = att.content_id
            try:
                await self._call("POST", self._url(mailbox, "messages", message_id, "attachments"),
                                 mailbox, headers=headers, json=payload, ok=(201,))
                return
            except DeliveryError as error:
                # Just under 3 MB the base64 request can top 4 MB; an upload session takes it.
                if error.status != 413:
                    raise

        item: dict[str, Any] = {
            "attachmentType": "file",
            "name": att.name,
            "size": att.size,
            "contentType": att.content_type,
            "isInline": att.is_inline,
        }
        if att.content_id:
            item["contentId"] = att.content_id
        _, session = await self._call(
            "POST", self._url(mailbox, "messages", message_id, "attachments", "createUploadSession"),
            mailbox, headers=headers, json={"AttachmentItem": item}, ok=(200, 201),
        )
        upload_url = session["uploadUrl"]
        for start in range(0, att.size, UPLOAD_CHUNK):
            chunk = att.data[start:start + UPLOAD_CHUNK]
            # The upload URL is pre-authenticated: no Authorization header.
            chunk_headers = {
                "Content-Type": "application/octet-stream",
                "Content-Range": f"bytes {start}-{start + len(chunk) - 1}/{att.size}",
            }
            await self._call("PUT", upload_url, mailbox, headers=chunk_headers, data=chunk, ok=(200, 201))

    async def _discard_draft(self, mailbox: str, message_id: str, headers: dict) -> None:
        try:
            await self._call("DELETE", self._url(mailbox, "messages", message_id), mailbox,
                             headers=headers, ok=(204,))
        except DeliveryError as error:
            log.warning(kv("graph draft-cleanup-failed", mailbox=mailbox, error=error))

    def ensure_app(self):
        """Build the MSAL app if needed (blocking: call from a thread). Raises if Microsoft says no."""
        with self._msal_lock:
            if self.app is None:
                if self._msal_factory is None:
                    raise RuntimeError("GraphClient has no MSAL app")
                self.app = self._msal_factory()
            return self.app

    def _acquire_token(self) -> dict:
        return self.ensure_app().acquire_token_for_client(scopes=[GRAPH_SCOPE])

    async def _auth_headers(self) -> dict[str, str]:
        try:
            result = await asyncio.to_thread(self._acquire_token)
        except Exception as error:
            detail = str(error).splitlines()[0][:200] if str(error) else ""
            message = f"token request failed: {type(error).__name__}: {detail}"
            self.record_auth(False, message)
            raise DeliveryError(message, transient=True) from error
        token = result.get("access_token") if isinstance(result, dict) else None
        if not token:
            result = result if isinstance(result, dict) else {}
            code = result.get("error", "unknown")
            description = (result.get("error_description") or "").splitlines()
            message = f"token request failed: {code}: {description[0][:200] if description else ''}"
            self.record_auth(False, message)
            raise DeliveryError(message, transient=True, code=code)
        self.record_auth(True)
        return {"Authorization": f"Bearer {token}"}

    def _url(self, mailbox: str, *parts: str) -> str:
        path = "/".join(quote(p, safe="=") for p in parts)
        return f"{self.base_url}/users/{quote(mailbox, safe='@')}/{path}"

    def _limit(self, mailbox: str) -> asyncio.Semaphore:
        if mailbox not in self._limits:
            self._limits[mailbox] = asyncio.Semaphore(MAILBOX_CONCURRENCY)
        return self._limits[mailbox]

    async def _call(self, method: str, url: str, mailbox: str, *, ok: tuple[int, ...], **kwargs) -> tuple[int, Any]:
        if self._session is None:
            raise RuntimeError("GraphClient used outside 'async with'")
        async with self._limit(mailbox):
            try:
                async with self._session.request(method, url, **kwargs) as response:
                    body = await _read_body(response)
                    if response.status in ok:
                        return response.status, body
                    raise _error(method, response, body)
            except (aiohttp.ClientError, TimeoutError) as error:
                raise DeliveryError(f"{method} {type(error).__name__}", transient=True) from error


async def _read_body(response: aiohttp.ClientResponse) -> Any:
    if response.content_type == "application/json":
        try:
            return await response.json()
        except (ValueError, aiohttp.ContentTypeError):
            return None
    text = await response.text(errors="replace")
    return text[:500] or None


def _error(method: str, response: aiohttp.ClientResponse, body: Any) -> DeliveryError:
    code = message = None
    if isinstance(body, dict) and isinstance(body.get("error"), dict):
        code = body["error"].get("code")
        message = (body["error"].get("message") or "")[:200]
    return DeliveryError(
        f"{method} {response.status} {code or ''} {message or ''}".strip(),
        transient=is_transient_status(response.status),
        status=response.status,
        code=code,
        retry_after=parse_retry_after(response.headers.get("Retry-After")),
    )
