"""
Small web dashboard: sign in with DASHBOARD_USER / DASHBOARD_PASS, see devices, messages,
the retry queue and the live log, and send a test message through the relay.
"""

from __future__ import annotations

import asyncio
import hmac
import html
import logging
import secrets
import smtplib
import ssl
import time
from email.message import EmailMessage
from pathlib import Path

from aiohttp import web

from activity import RETENTION_DAYS, ActivityLogHandler, ActivityStore
from config import Settings
from handlers.access import LoginLockout, normalize_mailbox, sender_allowed, valid_recipient
from logfmt import kv
from mail_queue import MailQueue
from version import __version__

log = logging.getLogger("relay")

STATIC_DIR = Path(__file__).resolve().parent / "dashboard_static"
STATIC_FILES = {"app.css": "text/css", "app.js": "text/javascript"}
COOKIE = "relay_session"
SESSION_IDLE = 8 * 3600
LOGIN_ERRORS = {
    "bad": "Wrong username or password.",
    "locked": "Too many failed sign-ins from this address. Try again in 15 minutes.",
}
SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; "
        "form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Cache-Control": "no-store",
}


def send_test_message(host: str, port: int, user: str, password: str, sender: str, to: str,
                      starttls: bool) -> dict:
    """Log in to the relay like a device would and send one short message. Blocking."""
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = to
    msg["Subject"] = f"smtp-graph-relay test message (v{__version__})"
    msg["X-Relay-Test"] = "dashboard"
    msg["X-Relay-Version"] = __version__
    msg.set_content(
        "This is a test message sent from the smtp-graph-relay dashboard at "
        f"{time.strftime('%Y-%m-%d %H:%M:%S %Z')}.\n\nIf you can read this, the relay can send as {sender}.\n\n"
        f"Relay version: {__version__}\n"
    )
    data = msg.as_bytes(policy=msg.policy.clone(linesep="\r\n"))
    stage = "connect"
    try:
        with smtplib.SMTP(host, port, timeout=90) as client:
            client.ehlo("dashboard-test")
            if starttls and client.has_extn("starttls"):
                context = ssl.create_default_context()
                # Talking to our own listener over loopback; its certificate may be self-signed.
                context.check_hostname = False
                context.verify_mode = ssl.CERT_NONE
                client.starttls(context=context)
                client.ehlo("dashboard-test")
            stage = "login"
            client.login(user, password)
            stage = "sender"
            code, reply = client.mail(sender)
            if code == 250:
                stage = "recipient"
                code, reply = client.rcpt(to)
                if code in (250, 251):
                    stage = "data"
                    code, reply = client.data(data)
            return {"ok": stage == "data" and code == 250, "stage": stage, "code": code,
                    "reply": reply.decode("utf-8", "replace")}
    except smtplib.SMTPAuthenticationError as error:
        return {"ok": False, "stage": "login", "code": error.smtp_code,
                "reply": error.smtp_error.decode("utf-8", "replace")}
    except (smtplib.SMTPException, OSError) as error:
        return {"ok": False, "stage": stage, "code": None, "reply": f"{type(error).__name__}: {error}"}


class Dashboard:
    def __init__(self, settings: Settings, *, store: ActivityStore, log_handler: ActivityLogHandler,
                 queue: MailQueue | None, smtp_host: str, smtp_port: int, tls_context: ssl.SSLContext | None,
                 graph=None, clock=time.time):
        self.settings = settings
        self.store = store
        self.log_handler = log_handler
        self.queue = queue
        self.graph = graph
        self.smtp_host = smtp_host
        self.smtp_port = smtp_port
        self.tls_context = tls_context
        self.https = settings.dashboard_https and tls_context is not None
        self._clock = clock
        self._sessions: dict[str, float] = {}
        self.lockout = LoginLockout()
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self._login_html = (STATIC_DIR / "login.html").read_text(encoding="utf-8").replace(
            "<!--version-->", __version__)
        self._index_html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")

    @property
    def port(self) -> int:
        return self._site._server.sockets[0].getsockname()[1]

    @property
    def url(self) -> str:
        host = "localhost" if self.settings.dashboard_host in ("0.0.0.0", "::") else self.settings.dashboard_host
        return f"{'https' if self.https else 'http'}://{host}:{self.port}/"

    def build_app(self) -> web.Application:
        app = web.Application(middlewares=[self._security_headers], client_max_size=64 * 1024)
        app.router.add_get("/", self.index)
        app.router.add_get("/login", self.login_page)
        app.router.add_post("/login", self.login)
        app.router.add_post("/logout", self.logout)
        app.router.add_get("/static/{name}", self.static)
        app.router.add_get("/api/summary", self.api_summary)
        app.router.add_post("/api/test-send", self.api_test_send)
        app.router.add_post("/api/queue/cancel", self.api_queue_cancel)
        return app

    async def start(self) -> None:
        self._runner = web.AppRunner(self.build_app(), access_log=None)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self.settings.dashboard_host, self.settings.dashboard_port,
                                 ssl_context=self.tls_context if self.https else None)
        await self._site.start()
        if self.settings.dashboard_https and not self.https:
            log.warning(kv("dashboard plain-http", reason="no-TLS-certificate-available"))
        log.info(kv("dashboard started", url=self.url))

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()

    # --- plumbing ---------------------------------------------------------------------

    @web.middleware
    async def _security_headers(self, request: web.Request, handler):
        response = await handler(request)
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        if self.https:
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
        return response

    def _signed_in(self, request: web.Request) -> bool:
        token = request.cookies.get(COOKIE)
        if not token:
            return False
        now = self._clock()
        expiry = self._sessions.get(token)
        if expiry is None or expiry < now:
            self._sessions.pop(token, None)
            return False
        self._sessions[token] = now + SESSION_IDLE  # idle timeout: each request extends it
        return True

    def _secure_cookie(self, request: web.Request) -> bool:
        return request.scheme == "https" or request.headers.get("X-Forwarded-Proto") == "https"

    @staticmethod
    def _redirect(location: str) -> web.Response:
        return web.Response(status=303, headers={"Location": location})

    # --- pages ------------------------------------------------------------------------

    async def index(self, request: web.Request) -> web.Response:
        if not self._signed_in(request):
            return self._redirect("/login")
        return web.Response(text=self._index_html, content_type="text/html")

    async def login_page(self, request: web.Request) -> web.Response:
        if self._signed_in(request):
            return self._redirect("/")
        message = LOGIN_ERRORS.get(request.query.get("error", ""))
        error = f'<p class="form-error" role="alert">{html.escape(message)}</p>' if message else ""
        return web.Response(text=self._login_html.replace("<!--error-->", error), content_type="text/html")

    async def login(self, request: web.Request) -> web.Response:
        ip = request.remote or "-"
        if self.lockout.is_locked(ip):
            log.warning(kv("dashboard login locked", ip=ip))
            return self._redirect("/login?error=locked")
        form = await request.post()
        username = str(form.get("username", "")).encode("utf-8")
        password = str(form.get("password", "")).encode("utf-8")
        user_ok = hmac.compare_digest(username, self.settings.dashboard_user.encode("utf-8"))
        pass_ok = hmac.compare_digest(password, self.settings.dashboard_pass.encode("utf-8"))
        if not (user_ok and pass_ok):
            fails = self.lockout.record_failure(ip)
            log.warning(kv("dashboard login failed", ip=ip, user=username.decode("utf-8", "replace"), fails=fails))
            return self._redirect("/login?error=locked" if self.lockout.is_locked(ip) else "/login?error=bad")
        self.lockout.record_success(ip)
        token = secrets.token_urlsafe(32)
        self._sessions[token] = self._clock() + SESSION_IDLE
        log.info(kv("dashboard login ok", ip=ip, user=self.settings.dashboard_user))
        response = self._redirect("/")
        response.set_cookie(COOKIE, token, httponly=True, samesite="Strict", secure=self._secure_cookie(request),
                            path="/", max_age=SESSION_IDLE)
        return response

    async def logout(self, request: web.Request) -> web.Response:
        token = request.cookies.get(COOKIE)
        if token:
            self._sessions.pop(token, None)
        response = self._redirect("/login")
        response.del_cookie(COOKIE, path="/")
        return response

    async def static(self, request: web.Request) -> web.Response:
        name = request.match_info["name"]
        if name not in STATIC_FILES:
            raise web.HTTPNotFound()
        return web.Response(body=(STATIC_DIR / name).read_bytes(), content_type=STATIC_FILES[name])

    # --- API --------------------------------------------------------------------------

    async def api_summary(self, request: web.Request) -> web.Response:
        if not self._signed_in(request):
            return web.json_response({"error": "signed out"}, status=401)
        s = self.settings
        summary = self.store.summary()
        jobs = self.queue.jobs() if self.queue is not None else []
        now = self._clock()
        summary["stats"]["queued"] = len(jobs)
        summary["stats"]["queue_oldest_seconds"] = int(now - jobs[0].first_seen) if jobs else None
        summary["queue"] = [
            {"id": j.id, "from": j.mailbox, "to": j.rcpt_tos, "ip": j.client_ip, "attempts": j.attempts,
             "next_attempt": j.next_attempt, "first_seen": j.first_seen, "last_error": j.last_error}
            for j in jobs
        ]
        summary["logs"] = list(self.log_handler.lines)[-300:]
        summary["relay"] = {
            "user": s.dashboard_user,
            "allowed_senders": list(s.allowed_senders),
            "sender_suggestions": self._sender_suggestions(summary.pop("recent_senders", [])),
            "starttls": self.tls_context is not None,
            "smtp_port": s.port,
            "large_messages": s.large_messages,
            "queue_enabled": self.queue is not None,
            "max_message_size": s.max_message_size,
            "microsoft": self.graph.auth_status if self.graph is not None else {"ok": None},
            "version": __version__,
            "retention_days": RETENTION_DAYS,
        }
        summary["generated"] = now
        return web.json_response(summary)

    async def api_queue_cancel(self, request: web.Request) -> web.Response:
        """Stop retrying one queued message ({"id": ...}) or all of them ({"all": true})."""
        if not self._signed_in(request):
            return web.json_response({"error": "signed out"}, status=401)
        if request.content_type != "application/json":
            return web.json_response({"error": "expected JSON"}, status=415)
        if self.queue is None:
            return web.json_response({"error": "the retry queue is off"}, status=409)
        try:
            body = await request.json()
            ids = [j.id for j in self.queue.jobs()] if body.get("all") is True else [str(body.get("id", ""))]
        except (ValueError, AttributeError):
            return web.json_response({"error": "expected a JSON object"}, status=400)

        user = self.settings.dashboard_user
        cancelled, skipped = [], []
        for job_id in ids:
            status, job = await asyncio.to_thread(self.queue.cancel, job_id, f"cancelled from the dashboard by {user}")
            if status == "cancelled":
                cancelled.append(job_id)
                log.info(kv("queue cancelled", **job.log_fields(), by=user, dashboard_ip=request.remote))
            else:
                skipped.append({"id": job_id, "status": status})
        return web.json_response({"cancelled": cancelled, "skipped": skipped})

    def _sender_suggestions(self, recent: list[str]) -> list[dict]:
        """Exact addresses from SMTP_ALLOWED_SENDERS first, then allowed senders that have sent before."""
        allowed = self.settings.allowed_senders
        suggestions = [{"address": a, "source": "allowed in .env"} for a in allowed if not a.startswith("*@")]
        seen = {s["address"] for s in suggestions}
        for address in recent:
            lowered = address.lower()
            if lowered not in seen and sender_allowed(lowered, allowed):
                suggestions.append({"address": lowered, "source": "used before"})
                seen.add(lowered)
        return suggestions[:12]

    async def api_test_send(self, request: web.Request) -> web.Response:
        if not self._signed_in(request):
            return web.json_response({"error": "signed out"}, status=401)
        if request.content_type != "application/json":
            return web.json_response({"error": "expected JSON"}, status=415)
        try:
            body = await request.json()
            sender = str(body.get("from", "")).strip()
            to = str(body.get("to", "")).strip()
        except (ValueError, AttributeError):
            return web.json_response({"error": "expected a JSON object"}, status=400)
        if normalize_mailbox(sender) is None:
            return web.json_response({"ok": False, "stage": "input", "reply": "From is not a valid address."})
        if not valid_recipient(to):
            return web.json_response({"ok": False, "stage": "input", "reply": "To is not a valid address."})

        s = self.settings
        result = await asyncio.to_thread(
            send_test_message, self.smtp_host, self.smtp_port, s.auth_user, s.auth_pass, sender, to,
            self.tls_context is not None,
        )
        if result["stage"] == "sender" and not sender_allowed(sender, s.allowed_senders):
            result["hint"] = f"Add {sender} to SMTP_ALLOWED_SENDERS in .env and restart the relay."
        elif result["ok"] and result["reply"].startswith("2.0.0 Queued"):
            result["hint"] = "Microsoft 365 did not take it right away; the queue keeps retrying."
        log.info(kv("dashboard test-send", ip=request.remote, **{"from": sender}, to=to, ok=result["ok"],
                    stage=result["stage"], code=result["code"]))
        return web.json_response(result)
