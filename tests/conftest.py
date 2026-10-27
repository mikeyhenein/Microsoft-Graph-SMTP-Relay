"""Shared fixtures: a fake Microsoft Graph, a stub MSAL app, and a running relay."""

from __future__ import annotations

import asyncio
import base64
import functools
import smtplib
import ssl
from dataclasses import dataclass, field, replace
from email.message import EmailMessage
from pathlib import Path

import pytest
from aiohttp import web

import retry
from config import Settings
from event_bus import event_bus_instance
from handlers import microsoft_graph
from smtp_server import Relay

PASSWORD = "correct-horse-battery-staple"


@dataclass
class Recorded:
    method: str
    path: str
    headers: dict
    body: bytes


@dataclass
class FakeGraph:
    """Records every request; replies come from per-route scripts, then per-route defaults."""

    requests: list[Recorded] = field(default_factory=list)
    scripts: dict[str, list] = field(default_factory=dict)
    defaults: dict[str, int] = field(default_factory=dict)
    url: str = ""
    root: str = ""
    _runner: web.AppRunner | None = None
    _uploads: int = 0

    SUCCESS = {"sendMail": 202, "create": 201, "attach": 201, "upload_session": 201,
               "upload": 200, "send": 202, "delete": 204}

    def script(self, route: str, *responses) -> None:
        """Queue replies: ints are statuses; (status, retry_after) tuples add a Retry-After header."""
        self.scripts.setdefault(route, []).extend(responses)

    def of(self, route_suffix: str, method: str = "POST") -> list[Recorded]:
        return [r for r in self.requests if r.method == method and r.path.endswith(route_suffix)]

    def _reply(self, route: str, ok_body=None) -> web.Response:
        queued = self.scripts.get(route)
        reply = queued.pop(0) if queued else self.defaults.get(route, self.SUCCESS[route])
        status, retry_after = reply if isinstance(reply, tuple) else (reply, None)
        if status >= 300:
            headers = {"Retry-After": str(retry_after)} if retry_after is not None else {}
            return web.json_response({"error": {"code": f"Fake{status}", "message": "scripted failure"}},
                                     status=status, headers=headers)
        if ok_body is None:
            return web.Response(status=status)
        return web.json_response(ok_body, status=status)

    async def _record(self, request: web.Request) -> None:
        self.requests.append(Recorded(request.method, request.path, dict(request.headers), await request.read()))

    async def start(self) -> None:
        async def handle(request: web.Request) -> web.Response:
            await self._record(request)
            path, method = request.path, request.method
            if path.startswith("/upload/"):
                return self._reply("upload", {})
            if path.endswith("/sendMail"):
                return self._reply("sendMail")
            if path.endswith("/attachments/createUploadSession"):
                self._uploads += 1
                return self._reply("upload_session", {"uploadUrl": f"{self.root}/upload/{self._uploads}"})
            if path.endswith("/attachments"):
                return self._reply("attach", {"id": "att"})
            if path.endswith("/send"):
                return self._reply("send")
            if path.endswith("/messages") and method == "POST":
                return self._reply("create", {"id": "AAMkDraft=="})
            if method == "DELETE":
                return self._reply("delete")
            return web.Response(status=404)

        app = web.Application(client_max_size=200 * 1024 * 1024)
        app.router.add_route("*", "/{tail:.*}", handle)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        self.root = f"http://127.0.0.1:{port}"
        self.url = f"{self.root}/v1.0"

    async def stop(self) -> None:
        if self._runner:
            await self._runner.cleanup()

    def sent_mime(self, index: int = -1) -> bytes:
        return base64.b64decode(self.of("/sendMail")[index].body)


class StubMsal:
    def __init__(self, result=None):
        self.result = result or {"access_token": "test-token", "token_type": "Bearer"}
        self.calls = 0

    def acquire_token_for_client(self, scopes):
        self.calls += 1
        assert scopes == ["https://graph.microsoft.com/.default"]
        return self.result


@pytest.fixture(autouse=True)
def clean_event_bus():
    event_bus_instance.clear()
    yield
    event_bus_instance.clear()


@pytest.fixture(autouse=True)
def fast_retries(monkeypatch):
    """Keep the in-session retry shape (3 tries) but without real 1s/4s waits."""
    monkeypatch.setattr(microsoft_graph, "deliver_now", functools.partial(retry.deliver_now, delays=(0.01, 0.02)))


@pytest.fixture
async def fake_graph():
    fake = FakeGraph()
    await fake.start()
    yield fake
    await fake.stop()


@pytest.fixture
def settings(tmp_path: Path, fake_graph: FakeGraph) -> Settings:
    return Settings(
        authority="https://login.microsoftonline.com/test-tenant",
        client_id="client",
        client_secret="secret",
        auth_user="relay",
        auth_pass=PASSWORD,
        allowed_senders=("scanner@contoso.com", "*@lulu.test"),
        hostname="127.0.0.1",
        port=0,
        data_dir=tmp_path,
        queue_dir=tmp_path / "queue",
        middleware_dir=tmp_path / "middleware",
        graph_base_url=fake_graph.url,
    )


@pytest.fixture
async def start_relay():
    """Start a relay with optional Settings overrides; every relay started is stopped at teardown."""
    relays: list[Relay] = []

    async def start(settings: Settings, msal_app=None, msal_factory=None, **overrides) -> Relay:
        if msal_app is None and msal_factory is None:
            msal_app = StubMsal()
        relay = Relay(replace(settings, **overrides), msal_app=msal_app, msal_factory=msal_factory)
        await relay.start()
        relays.append(relay)
        return relay

    yield start
    for relay in relays:
        await relay.stop(grace=1)


@pytest.fixture
async def relay(settings, start_relay):
    return await start_relay(settings)


def make_message(sender="scanner@contoso.com", to="boss@example.com", subject="Scan", attachment: bytes | None = None,
                 **headers) -> bytes:
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = to
    msg["Subject"] = subject
    for name, value in headers.items():
        msg[name.replace("_", "-")] = value
    msg.set_content("Plain body")
    msg.add_alternative("<p>HTML body</p>", subtype="html")
    if attachment is not None:
        msg.add_attachment(attachment, maintype="application", subtype="pdf", filename="scan.pdf")
    return msg.as_bytes(policy=msg.policy.clone(linesep="\r\n"))  # SMTP wire format, like a real device


@dataclass
class SmtpResult:
    mail: tuple[int, bytes] | None = None
    rcpt: list[tuple[int, bytes]] = field(default_factory=list)
    data: tuple[int, bytes] | None = None
    login: tuple[int, bytes] | None = None


def smtp_exchange(port: int, *, sender="scanner@contoso.com", rcpts=("boss@example.com",), message: bytes | None = None,
                  user: str | None = "relay", password: str = PASSWORD, starttls: bool = False) -> SmtpResult:
    """One SMTP conversation; returns every reply instead of raising on rejections."""
    result = SmtpResult()
    with smtplib.SMTP("127.0.0.1", port, timeout=15) as client:
        client.ehlo("test-client")
        if starttls:
            context = ssl.create_default_context()
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            client.starttls(context=context)
            client.ehlo("test-client")
        if user is not None:
            try:
                result.login = client.login(user, password)
            except smtplib.SMTPAuthenticationError as error:
                result.login = (error.smtp_code, error.smtp_error)
                return result
        result.mail = client.mail(sender)
        if result.mail[0] != 250:
            return result
        result.rcpt = [client.rcpt(r) for r in rcpts]
        if not any(code == 250 for code, _ in result.rcpt):
            return result
        result.data = client.data(message if message is not None else make_message(sender=sender))
    return result


async def exchange(port: int, **kwargs) -> SmtpResult:
    return await asyncio.to_thread(smtp_exchange, port, **kwargs)
