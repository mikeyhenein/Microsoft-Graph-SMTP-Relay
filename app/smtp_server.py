"""The SMTP listener, middleware loading, and the relay's start/stop lifecycle."""

from __future__ import annotations

import asyncio
import importlib.util
import ipaddress
import logging
import secrets
import socket
import ssl
import warnings
from pathlib import Path

from aiosmtpd.smtp import SMTP, TLSSetupException
from msal import ConfidentialClientApplication

import hostnames
from activity import ActivityLogHandler, ActivityStore
from config import Settings
from dashboard import Dashboard
from handlers.authenticator import Authenticator
from handlers.graph_client import GraphClient
from handlers.microsoft_graph import MicrosoftGraphHandler
from logfmt import kv, peer_ip
from mail_queue import MailQueue
from tls import load_tls_context
from version import __version__

log = logging.getLogger("relay")

class _DropAiosmtpdNoise(logging.Filter):
    """
    Plaintext AUTH is allowed on purpose (older scanners cannot do STARTTLS) and each such
    login is logged as a warning by the relay; aiosmtpd would also warn on every connection.
    It also warns about its own deprecated `login_data` on every login.
    """

    NOISE = ("auth_require_tls == False", "Session.login_data is deprecated")

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        return not any(n in message for n in self.NOISE)


logging.getLogger("mail.log").addFilter(_DropAiosmtpdNoise())


def _is_loopback(ip: str) -> bool:
    try:
        return ipaddress.ip_address(ip).is_loopback
    except ValueError:
        return False


# OpenSSL reasons seen on the server side when a client gives up on the handshake.
TLS_FAILURE_REASONS = {
    "TLSV1_ALERT_UNKNOWN_CA": "untrusted-certificate",
    "SSLV3_ALERT_BAD_CERTIFICATE": "untrusted-certificate",
    "SSLV3_ALERT_CERTIFICATE_UNKNOWN": "untrusted-certificate",
    "TLSV1_ALERT_BAD_CERTIFICATE": "untrusted-certificate",
    "UNSUPPORTED_PROTOCOL": "old-tls-version",
    "VERSION_TOO_LOW": "old-tls-version",
    "TLSV1_ALERT_PROTOCOL_VERSION": "old-tls-version",
    "NO_SHARED_CIPHER": "no-shared-cipher",
    "UNEXPECTED_EOF_WHILE_READING": "aborted",
}


def classify_tls_failure(error: BaseException | None) -> tuple[str, str]:
    """(reason code, detail) for a failed STARTTLS handshake."""
    if isinstance(error, ssl.SSLError):
        code = getattr(error, "reason", None) or type(error).__name__
        if isinstance(error, ssl.SSLEOFError):
            return "aborted", code
        return TLS_FAILURE_REASONS.get(code, "handshake-failed"), code
    if isinstance(error, (ConnectionError, EOFError)):
        return "aborted", type(error).__name__
    return "handshake-failed", type(error).__name__ if error else "unknown"


def _looks_like_tls_hello(data: bytes) -> bool:
    # A TLS ClientHello record starts 0x16 0x03; SMTP clients start with text ("EHLO ...").
    return len(data) >= 2 and data[0] == 0x16 and data[1] == 0x03


class RelaySMTP(SMTP):
    """
    aiosmtpd's SMTP with a session id, a connect/disconnect line per client, a background
    reverse-DNS lookup of the client, and one clear log line when TLS setup fails.
    """

    def __init__(self, *args, **kwargs):
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", message="Requiring AUTH while not requiring TLS")
            super().__init__(*args, **kwargs)
        self.sid = secrets.token_hex(3)
        self._connected = False
        self._saw_data = False
        self.hostname_task: asyncio.Task | None = None

    def _create_session(self):
        # aiosmtpd makes a fresh Session after STARTTLS; the id and name lookup stay with the connection.
        session = super()._create_session()
        session.sid = self.sid
        session.hostname_task = self.hostname_task
        return session

    def connection_made(self, transport):
        first = not self._connected
        super().connection_made(transport)
        if first:
            self._connected = True
            peer = transport.get_extra_info("peername")
            ip = peer_ip(self.session)
            if not _is_loopback(ip):
                self.hostname_task = self.loop.create_task(hostnames.lookup(ip))
                self.session.hostname_task = self.hostname_task
            # The container healthcheck connects over loopback every 30s; keep that out of INFO.
            level = logging.DEBUG if _is_loopback(ip) else logging.INFO
            log.log(level, kv("connect", sid=self.sid, ip=ip, port=peer[1] if isinstance(peer, tuple) else None))

    def data_received(self, data: bytes) -> None:
        if not self._saw_data:
            self._saw_data = True
            if _looks_like_tls_hello(data):
                # The device opened with TLS instead of plain SMTP ("SSL/TLS", usually meant for port 465).
                self._log_tls_failure("implicit-tls", "TLS ClientHello before any SMTP command")
                self.transport.close()
                return
        super().data_received(data)

    async def handle_exception(self, error: Exception) -> str:
        if isinstance(error, TLSSetupException):
            reason, detail = classify_tls_failure(error.__cause__)
            self._log_tls_failure(reason, detail)
            return "454 4.7.0 TLS not available"  # the connection is closed right after this
        return await super().handle_exception(error)

    def _log_tls_failure(self, reason: str, detail: str) -> None:
        session = self.session
        log.warning(kv("tls failed", sid=self.sid, ip=peer_ip(session), name=getattr(session, "host_name", None),
                       host=hostnames.known_name(session), reason=reason, detail=detail))

    def connection_lost(self, error):
        if self._connected:
            self._connected = False
            ip = peer_ip(self.session) if self.session else "-"
            level = logging.DEBUG if _is_loopback(ip) else logging.INFO
            log.log(level, kv("disconnect", sid=self.sid, ip=ip))
        super().connection_lost(error)


def load_middleware(directory: Path, handler: MicrosoftGraphHandler) -> list[str]:
    """Import every *.py in `directory` and instantiate its `Middleware(handler)` class."""
    loaded: list[str] = []
    if not directory.is_dir():
        log.info(kv("middleware none", dir=directory, reason="directory-missing"))
        return loaded
    for path in sorted(directory.glob("*.py")):
        if path.name.startswith("_"):
            continue
        name = f"relay_middleware_{path.stem}"
        try:
            spec = importlib.util.spec_from_file_location(name, path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        except Exception:
            log.exception(kv("middleware failed", file=path.name, stage="import"))
            continue
        middleware_class = getattr(module, "Middleware", None)
        if middleware_class is None:
            log.info(kv("middleware skipped", file=path.name, reason="no-Middleware-class"))
            continue
        try:
            middleware_class(handler)
        except Exception:
            log.exception(kv("middleware failed", file=path.name, stage="init"))
            continue
        loaded.append(path.name)
    log.info(kv("middleware loaded", dir=directory, modules=",".join(loaded) or "none"))
    return loaded


class Relay:
    """Owns everything that lives for the life of the process."""

    def __init__(self, settings: Settings, msal_app=None, msal_factory=None):
        self.settings = settings
        self.msal_app = msal_app
        self.msal_factory = msal_factory
        self.server: asyncio.Server | None = None
        self.handler: MicrosoftGraphHandler | None = None
        self.queue: MailQueue | None = None
        self.dashboard: Dashboard | None = None
        self._graph: GraphClient | None = None
        self._worker: asyncio.Task | None = None
        self._activity: ActivityStore | None = None
        self._log_handler: ActivityLogHandler | None = None

    @property
    def port(self) -> int:
        return self.server.sockets[0].getsockname()[1]

    async def start(self) -> None:
        s = self.settings
        loop = asyncio.get_running_loop()
        if s.dashboard_enabled:
            # Attach first so the dashboard's live log includes the startup lines.
            self._activity = ActivityStore(s.data_dir / "activity.db")
            self._log_handler = ActivityLogHandler(self._activity)
            logging.getLogger().addHandler(self._log_handler)
            relay_log = logging.getLogger("relay")
            if relay_log.getEffectiveLevel() > logging.INFO:
                relay_log.setLevel(logging.INFO)  # the dashboard needs INFO events even if stdout shows less
        # Looked up once: aiosmtpd would otherwise do a DNS lookup on every connection.
        banner_host = socket.gethostname()
        tls_context = load_tls_context(s.tls_cert_file, s.tls_key_file, s.data_dir, banner_host)

        def build_msal():
            return ConfidentialClientApplication(
                client_id=s.client_id, client_credential=s.client_secret, authority=s.authority
            )

        self._graph = GraphClient(self.msal_app, msal_factory=self.msal_factory or build_msal,
                                  base_url=s.graph_base_url, large_messages=s.large_messages)
        await self._graph.__aenter__()
        try:
            await asyncio.to_thread(self._graph.ensure_app)
        except Exception as error:
            # Keep serving: sends will retry the sign-in setup and queue mail meanwhile.
            detail = str(error).splitlines()[0][:300]
            self._graph.record_auth(False, f"sign-in setup failed: {detail}")
            log.error(kv("graph setup-failed", authority=s.authority, error=detail,
                         effect="mail-will-queue-until-Microsoft-sign-in-works"))
        self.queue = MailQueue.open(s.queue_dir, s.queue_max_age_hours)
        self.handler = MicrosoftGraphHandler(s, self._graph, self.queue)
        load_middleware(s.middleware_dir, self.handler)
        authenticator = Authenticator(s.auth_user, s.auth_pass)

        def protocol():
            return RelaySMTP(
                self.handler,
                hostname=banner_host,
                ident="smtp-graph-relay",
                data_size_limit=s.max_message_size,
                tls_context=tls_context,
                require_starttls=False,
                auth_required=True,
                auth_require_tls=False,
                authenticator=authenticator,
                loop=loop,
            )

        self.server = await loop.create_server(protocol, host=s.hostname, port=s.port)
        if self.queue is not None:
            self._worker = asyncio.create_task(self.queue.run(self.handler.deliver_queued))
        for warning in s.warnings:
            log.warning(kv("config", warning=warning))
        log.info(kv("relay started", version=__version__, starttls="yes" if tls_context else "no",
                    **{"settings": s.summary()}))

        if s.dashboard_enabled:
            if not self._activity.persistent:
                log.warning(kv("dashboard history in-memory", reason="cannot-write", path=s.data_dir / "activity.db"))
            self.dashboard = Dashboard(
                s, store=self._activity, log_handler=self._log_handler, queue=self.queue, graph=self._graph,
                smtp_host="127.0.0.1" if s.hostname in ("0.0.0.0", "", "::") else s.hostname,
                smtp_port=self.port, tls_context=tls_context,
            )
            await self.dashboard.start()
        else:
            log.info(kv("dashboard off", reason="DASHBOARD_USER-and-DASHBOARD_PASS-not-set"))

    async def stop(self, grace: float = 30.0) -> None:
        if self.dashboard is not None:
            await self.dashboard.stop()
            self.dashboard = None
        if self.server is not None:
            self.server.close()  # stop accepting new connections
        if self.handler is not None and not await self.handler.drain(grace):
            log.warning(kv("relay stopping", note="in-flight sends did not finish in time"))
        if self._worker is not None:
            self._worker.cancel()
            await asyncio.gather(self._worker, return_exceptions=True)
        if self.server is not None:
            self.server.close_clients()
        if self._graph is not None:
            await self._graph.__aexit__(None, None, None)
        log.info("relay stopped")
        if self._log_handler is not None:
            logging.getLogger().removeHandler(self._log_handler)
            self._log_handler = None
        if self._activity is not None:
            self._activity.close()
            self._activity = None
