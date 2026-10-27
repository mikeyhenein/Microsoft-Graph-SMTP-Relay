"""Settings, read once from the environment and validated at startup."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent

GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"
GRAPH_SCOPE = "https://graph.microsoft.com/.default"

# Graph rejects write requests over 4 MB. A MIME body grows by 4/3 when base64 encoded,
# so raw messages up to ~3 MB fit in one sendMail call.
SMALL_MESSAGE_LIMIT = 3_000_000
# Exchange Online's default per-message limit; admins can raise it to 150 MB.
LARGE_MESSAGE_DEFAULT = 35 * 1024 * 1024
LARGE_MESSAGE_CEILING = 150 * 1024 * 1024

MIN_PASSWORD_LENGTH = 16

_SENDER_PATTERN = re.compile(r"^(\*|[A-Za-z0-9._+'-]+)@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$")
_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off", ""}


class ConfigError(Exception):
    """Every problem found in the environment, reported together."""

    def __init__(self, problems: list[str]):
        super().__init__("\n".join(problems))
        self.problems = problems


@dataclass(frozen=True)
class Settings:
    authority: str
    client_id: str
    client_secret: str = field(repr=False)
    auth_user: str
    auth_pass: str = field(repr=False)
    allowed_senders: tuple[str, ...]
    hostname: str = "0.0.0.0"
    port: int = 2525
    data_dir: Path = Path("/data")
    tls_cert_file: Path | None = None
    tls_key_file: Path | None = None
    large_messages: bool = False
    max_message_size: int = SMALL_MESSAGE_LIMIT
    queue_dir: Path = Path("/data/queue")
    queue_max_age_hours: float = 24.0
    middleware_dir: Path = APP_DIR / "middleware"
    log_level: str = "INFO"
    log_file: Path | None = None
    dashboard_user: str = ""
    dashboard_pass: str = field(default="", repr=False)
    dashboard_host: str = "0.0.0.0"
    dashboard_port: int = 8080
    dashboard_https: bool = True
    graph_base_url: str = GRAPH_BASE_URL
    warnings: tuple[str, ...] = ()

    @property
    def dashboard_enabled(self) -> bool:
        return bool(self.dashboard_user and self.dashboard_pass)

    def summary(self) -> str:
        """One line for the startup log. No secrets."""
        dashboard = f"{self.dashboard_host}:{self.dashboard_port}" if self.dashboard_enabled else "off"
        return (
            f"listen={self.hostname}:{self.port} user={self.auth_user} "
            f"allowed_senders={','.join(self.allowed_senders)} large_messages={self.large_messages} "
            f"max_message_size={self.max_message_size} queue_dir={self.queue_dir} "
            f"queue_max_age_hours={self.queue_max_age_hours:g} middleware_dir={self.middleware_dir} "
            f"dashboard={dashboard}"
        )


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    env = os.environ if env is None else env
    problems: list[str] = []
    warnings: list[str] = []

    def get(name: str, default: str = "") -> str:
        return env.get(name, default).strip()

    def required(name: str) -> str:
        value = get(name)
        if not value:
            problems.append(f"{name} is required")
        return value

    def boolean(name: str, default: bool) -> bool:
        raw = get(name).lower()
        if not raw:
            return default
        if raw in _TRUE:
            return True
        if raw in _FALSE:
            return False
        problems.append(f"{name} must be true or false, got {raw!r}")
        return default

    def integer(name: str, default: int, low: int, high: int) -> int:
        raw = get(name)
        if not raw:
            return default
        try:
            value = int(raw)
        except ValueError:
            problems.append(f"{name} must be a whole number, got {raw!r}")
            return default
        if not low <= value <= high:
            problems.append(f"{name} must be between {low} and {high}, got {value}")
        return value

    authority = required("AUTHORITY")
    if authority and not authority.startswith("https://"):
        problems.append("AUTHORITY must look like https://login.microsoftonline.com/<tenant-id>")
    client_id = required("CLIENT_ID")
    client_secret = required("CLIENT_SECRET")

    auth_user = get("SMTP_AUTH_USER")
    auth_pass = env.get("SMTP_AUTH_PASS", "")
    if not auth_user or not auth_pass:
        problems.append(
            "SMTP_AUTH_USER and SMTP_AUTH_PASS are required: every device must log in "
            "(running without a login would make this an open relay)"
        )
    elif len(auth_pass) < MIN_PASSWORD_LENGTH:
        warnings.append(
            f"SMTP_AUTH_PASS is shorter than {MIN_PASSWORD_LENGTH} characters; "
            "devices without STARTTLS send it unencrypted"
        )

    allowed = tuple(
        s.lower() for s in re.split(r"[\s,]+", get("SMTP_ALLOWED_SENDERS")) if s
    )
    if not allowed:
        problems.append(
            "SMTP_ALLOWED_SENDERS is required: the From addresses this login may send as "
            "(e.g. scanner@contoso.com or *@contoso.com)"
        )
    for pattern in allowed:
        if not _SENDER_PATTERN.match(pattern):
            problems.append(f"SMTP_ALLOWED_SENDERS entry {pattern!r} is not an address or *@domain")

    port = integer("SMTP_RELAY_PORT", 2525, 1, 65535)
    data_dir = Path(get("DATA_DIR") or "/data")

    cert = get("TLS_CERT_FILE")
    key = get("TLS_KEY_FILE")
    if bool(cert) != bool(key):
        problems.append("TLS_CERT_FILE and TLS_KEY_FILE must be set together")

    large = boolean("LARGE_MESSAGES", False)
    default_size = LARGE_MESSAGE_DEFAULT if large else SMALL_MESSAGE_LIMIT
    max_size = integer("MAX_MESSAGE_SIZE", default_size, 1024, LARGE_MESSAGE_CEILING)
    if not large and max_size > SMALL_MESSAGE_LIMIT:
        problems.append(
            f"MAX_MESSAGE_SIZE above {SMALL_MESSAGE_LIMIT} needs LARGE_MESSAGES=true "
            "(and the Mail.ReadWrite permission)"
        )

    queue_dir = Path(get("QUEUE_DIR") or data_dir / "queue")
    max_age_raw = get("QUEUE_MAX_AGE_HOURS") or "24"
    try:
        max_age = float(max_age_raw)
        if not 0 < max_age <= 24 * 30:
            raise ValueError
    except ValueError:
        problems.append(f"QUEUE_MAX_AGE_HOURS must be a number of hours (up to 720), got {max_age_raw!r}")
        max_age = 24.0

    middleware_dir = Path(get("MIDDLEWARE_DIR")) if get("MIDDLEWARE_DIR") else APP_DIR / "middleware"
    if not middleware_dir.is_absolute():
        middleware_dir = APP_DIR / middleware_dir

    log_level = (get("LOG_LEVEL") or "INFO").upper()
    if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR"}:
        problems.append(f"LOG_LEVEL must be DEBUG, INFO, WARNING or ERROR, got {log_level!r}")
    log_file = None
    if boolean("LOG_FILE_ENABLED", False):
        log_file = Path(get("LOG_FILE") or data_dir / "logs" / "smtp_relay.log")

    dashboard_user = get("DASHBOARD_USER")
    dashboard_pass = env.get("DASHBOARD_PASS", "")
    dashboard_port = integer("DASHBOARD_PORT", 8080, 1, 65535)
    dashboard_https = boolean("DASHBOARD_HTTPS", True)
    if bool(dashboard_user) != bool(dashboard_pass):
        problems.append("DASHBOARD_USER and DASHBOARD_PASS must be set together (leave both empty to turn it off)")
    elif dashboard_pass:
        if len(dashboard_pass) < MIN_PASSWORD_LENGTH:
            warnings.append(f"DASHBOARD_PASS is shorter than {MIN_PASSWORD_LENGTH} characters")
        if dashboard_pass == auth_pass:
            warnings.append("DASHBOARD_PASS is the same as SMTP_AUTH_PASS; give the dashboard its own password")
        if dashboard_port == port:
            problems.append("DASHBOARD_PORT must differ from SMTP_RELAY_PORT")

    if get("SAVE_TO_SENT"):
        warnings.append("SAVE_TO_SENT is no longer used: a copy of every message is kept in Sent Items")

    if problems:
        raise ConfigError(problems)

    return Settings(
        authority=authority,
        client_id=client_id,
        client_secret=client_secret,
        auth_user=auth_user,
        auth_pass=auth_pass,
        allowed_senders=allowed,
        hostname=get("SMTP_RELAY_HOSTNAME") or "0.0.0.0",
        port=port,
        data_dir=data_dir,
        tls_cert_file=Path(cert) if cert else None,
        tls_key_file=Path(key) if key else None,
        large_messages=large,
        max_message_size=max_size,
        queue_dir=queue_dir,
        queue_max_age_hours=max_age,
        middleware_dir=middleware_dir,
        log_level=log_level,
        log_file=log_file,
        dashboard_user=dashboard_user,
        dashboard_pass=dashboard_pass,
        dashboard_host=get("DASHBOARD_HOST") or "0.0.0.0",
        dashboard_port=dashboard_port,
        dashboard_https=dashboard_https,
        warnings=tuple(warnings),
    )
