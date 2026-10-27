"""
What the dashboard shows: recent messages, devices by IP, and the live log.

Fed entirely by the relay's own log lines (a logging handler parses the key=value
lines), so the dashboard always matches `docker logs`. History is kept in a small
SQLite file on the data volume so it survives restarts.
"""

from __future__ import annotations

import collections
import logging
import re
import sqlite3
import threading
import time
from pathlib import Path

RETENTION_DAYS = 30
DEVICE_RETENTION_DAYS = 30
LOG_LINES = 500
DAY = 86400.0

# Rejections decided by the relay's own rules (as opposed to Microsoft 365 refusing).
RELAY_RULE_REASONS = {"sender-not-allowed", "invalid-sender", "from-header-mismatch", "unparseable",
                      "invalid-recipient", "middleware-error", "skip_send"}

RESULT_LABELS = {"sent": "Sent", "queued": "Queued", "rejected": "Rejected", "deferred": "Deferred",
                 "skipped": "Skipped", "failed": "Failed", "cancelled": "Cancelled"}

_FIELD = re.compile(r'(?:^|\s)([A-Za-z_][A-Za-z0-9_]*)=("(?:[^"\\]|\\.)*"|\S*)')

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    sid TEXT, ip TEXT, user TEXT, sender TEXT, rcpts TEXT, size INTEGER, tls TEXT,
    result TEXT, graph TEXT, attempts INTEGER, queue_id TEXT, reason TEXT, name TEXT
);
CREATE INDEX IF NOT EXISTS messages_ts ON messages (ts);
CREATE INDEX IF NOT EXISTS messages_queue_id ON messages (queue_id);
CREATE TABLE IF NOT EXISTS devices (
    ip TEXT PRIMARY KEY,
    name TEXT, first_seen REAL, last_seen REAL, tls TEXT, last_result TEXT, last_kind TEXT,
    hostname TEXT, last_reason TEXT
);
"""
# Columns added after the first release; ALTER TABLE brings older activity.db files up to date.
LATER_COLUMNS = (("devices", "hostname TEXT"), ("devices", "last_reason TEXT"), ("messages", "name TEXT"))

TLS_LABELS = {
    "untrusted-certificate": "TLS failed: certificate not trusted",
    "old-tls-version": "TLS failed: device's TLS version too old",
    "implicit-tls": "TLS failed: device expects SSL/TLS on connect",
    "no-shared-cipher": "TLS failed: no common cipher",
    "aborted": "TLS failed: device hung up during setup",
    "handshake-failed": "TLS failed",
}


def parse_kv(text: str) -> tuple[str, dict[str, str | None]]:
    """'auth ok ip=1.2.3.4 user=relay' -> ('auth ok', {'ip': '1.2.3.4', 'user': 'relay'})."""
    first = _FIELD.search(text)
    if first is None:
        return text.strip(), {}
    fields: dict[str, str | None] = {}
    for match in _FIELD.finditer(text):
        value = match.group(2)
        if len(value) >= 2 and value[0] == value[-1] == '"':
            value = value[1:-1].replace('\\"', '"')
        fields[match.group(1)] = None if value in ("", "-") else value
    return text[: first.start()].strip(), fields


def _int(value: str | None) -> int | None:
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


def _kind(result: str | None) -> str:
    if result in ("sent",):
        return "sent"
    if result in ("queued", "deferred"):
        return "queued"
    if result in ("rejected", "failed", "locked", "login-failed"):
        return "rejected"
    return "neutral"


class ActivityStore:
    def __init__(self, path: Path | None, clock=time.time):
        self._clock = clock
        self._lock = threading.Lock()
        target = ":memory:"
        if path is not None:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                target = str(path)
            except OSError:
                target = ":memory:"
        try:
            self._db = sqlite3.connect(target, check_same_thread=False, isolation_level=None)
        except sqlite3.Error:
            target = ":memory:"
            self._db = sqlite3.connect(target, check_same_thread=False, isolation_level=None)
        self.persistent = target != ":memory:"
        self._db.row_factory = sqlite3.Row
        if self.persistent:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.executescript(SCHEMA)
        for table, column in LATER_COLUMNS:
            try:
                self._db.execute(f"ALTER TABLE {table} ADD COLUMN {column}")
            except sqlite3.OperationalError:
                pass  # already there
        self._last_prune = 0.0
        self.prune()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def prune(self) -> None:
        now = self._clock()
        with self._lock:
            self._db.execute("DELETE FROM messages WHERE ts < ?", (now - RETENTION_DAYS * DAY,))
            self._db.execute("DELETE FROM devices WHERE last_seen < ?", (now - DEVICE_RETENTION_DAYS * DAY,))
        self._last_prune = now

    # --- writing (from the log handler) ------------------------------------------------

    def record(self, event: str, f: dict[str, str | None], ts: float) -> None:
        ip = f.get("ip")
        with self._lock:
            if event == "message":
                result = f.get("result")
                self._insert_message(ts, f, result, f.get("from"), f.get("to"), f.get("reason"))
                reason = None
                if result in ("sent", "queued"):
                    reason = ""  # working again: clear any earlier problem
                elif result == "rejected":
                    reason = f.get("reason") if f.get("reason") in RELAY_RULE_REASONS else "microsoft-refused"
                self._device(ip, ts, tls=f.get("tls"), result=RESULT_LABELS.get(result or "", "?"),
                             kind=_kind(result), reason=reason)
            elif event == "reject" and f.get("cmd") == "MAIL":
                self._insert_message(ts, f, "rejected", f.get("address"), None, f.get("reason"))
                self._device(ip, ts, result="Sender not allowed" if f.get("reason") == "sender-not-allowed"
                             else "Rejected", kind="rejected", reason=f.get("reason"))
            elif event == "helo":
                self._device(ip, ts, name=f.get("name"), hostname=f.get("host"))
            elif event == "connect":
                self._device(ip, ts)
            elif event == "tls failed":
                reason = f.get("reason") or "handshake-failed"
                self._device(ip, ts, name=f.get("name"), hostname=f.get("host"),
                             result=TLS_LABELS.get(reason, "TLS failed"), kind="rejected", reason=f"tls-{reason}")
            elif event == "auth ok":
                self._device(ip, ts, tls=f.get("tls"), result="Logged in", kind="neutral", reason="")
            elif event == "auth failed":
                fails = f.get("fails")
                self._device(ip, ts, result=f"Login failed ({fails})" if fails else "Login failed", kind="rejected",
                             reason="login-failed")
            elif event == "auth locked":
                self._device(ip, ts, result="Locked out · too many bad logins", kind="rejected", reason="locked")
            elif event == "queue sent":
                self._db.execute(
                    "UPDATE messages SET result='sent', graph=?, attempts=COALESCE(attempts, 0) + ? WHERE queue_id=?",
                    (f.get("graph"), _int(f.get("attempts")) or 1, f.get("id")),
                )
            elif event == "queue failed":
                self._db.execute("UPDATE messages SET result='failed', reason=? WHERE queue_id=?",
                                 (f.get("reason"), f.get("id")))
            elif event == "queue cancelled":
                self._db.execute("UPDATE messages SET result='cancelled', reason='cancelled' WHERE queue_id=?",
                                 (f.get("id"),))
        if ts - self._last_prune > 3600:
            self.prune()

    def _insert_message(self, ts, f, result, sender, rcpts, reason) -> None:
        self._db.execute(
            "INSERT INTO messages (ts, sid, ip, user, sender, rcpts, size, tls, result, graph, attempts, queue_id,"
            " reason, name) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (ts, f.get("sid"), f.get("ip"), f.get("user"), sender, rcpts, _int(f.get("size")), f.get("tls"),
             result, f.get("graph"), _int(f.get("attempts")), f.get("queue_id"), reason, f.get("name")),
        )

    def _device(self, ip, ts, *, name=None, hostname=None, tls=None, result=None, kind=None, reason=None) -> None:
        """Upsert a device. None leaves a field as it was; reason "" clears the device's problem."""
        if not ip:
            return
        self._db.execute(
            "INSERT INTO devices (ip, name, hostname, first_seen, last_seen, tls, last_result, last_kind, last_reason)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(ip) DO UPDATE SET last_seen=excluded.last_seen,"
            " name=COALESCE(excluded.name, devices.name), hostname=COALESCE(excluded.hostname, devices.hostname),"
            " tls=COALESCE(excluded.tls, devices.tls),"
            " last_result=COALESCE(excluded.last_result, devices.last_result),"
            " last_kind=COALESCE(excluded.last_kind, devices.last_kind),"
            " last_reason=COALESCE(excluded.last_reason, devices.last_reason)",
            (ip, name, hostname, ts, ts, tls, result, kind, reason),
        )

    # --- reading (for the dashboard) ---------------------------------------------------

    def summary(self) -> dict:
        now = self._clock()
        since = now - DAY
        with self._lock:
            db = self._db
            sent = db.execute("SELECT COUNT(*) FROM messages WHERE ts >= ? AND result='sent'", (since,)).fetchone()[0]
            retried = db.execute(
                "SELECT COUNT(*) FROM messages WHERE ts >= ? AND result='sent'"
                " AND (attempts > 1 OR queue_id IS NOT NULL)",
                (since,),
            ).fetchone()[0]
            rejected_rows = db.execute(
                "SELECT reason, graph FROM messages WHERE ts >= ? AND result IN ('rejected', 'failed')", (since,)
            ).fetchall()
            devices = [dict(r) for r in db.execute(
                "SELECT d.ip, d.name, d.hostname, d.last_seen, d.tls, d.last_result, d.last_kind,"
                " NULLIF(d.last_reason, '') AS last_reason,"
                " (SELECT COUNT(*) FROM messages m WHERE m.ip = d.ip AND m.ts >= ?) AS messages_24h"
                " FROM devices d WHERE d.last_seen >= ? ORDER BY d.last_seen DESC LIMIT 200",
                (since, now - RETENTION_DAYS * DAY),
            )]
            messages = [dict(r) for r in db.execute(
                "SELECT ts, ip, name, user, sender, rcpts, size, tls, result, graph, attempts, queue_id, reason"
                " FROM messages ORDER BY ts DESC LIMIT 50"
            )]
            recent_senders = [r[0] for r in db.execute(
                "SELECT sender FROM messages WHERE sender IS NOT NULL AND result IN ('sent', 'queued')"
                " GROUP BY lower(sender) ORDER BY MAX(ts) DESC LIMIT 20"
            )]
        by_relay = sum(1 for r in rejected_rows if (r["reason"] or "") in RELAY_RULE_REASONS or not r["graph"])
        recent_devices = [d for d in devices if d["last_seen"] >= since]
        return {
            "stats": {
                "sent": sent,
                "retried": retried,
                "rejected": len(rejected_rows),
                "rejected_by_relay": by_relay,
                "rejected_by_microsoft": len(rejected_rows) - by_relay,
                "devices": len(recent_devices),
                "devices_without_tls": sum(1 for d in recent_devices if d["tls"] == "no"),
            },
            "devices": devices,
            "messages": messages,
            "recent_senders": recent_senders,
            "persistent": self.persistent,
        }


class ActivityLogHandler(logging.Handler):
    """Keeps the last LOG_LINES lines for the live log and feeds relay events to the store."""

    def __init__(self, store: ActivityStore | None, max_lines: int = LOG_LINES):
        super().__init__(level=logging.INFO)
        self.store = store
        self.lines: collections.deque[dict] = collections.deque(maxlen=max_lines)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if record.name != "relay" and record.levelno < logging.WARNING:
                return
            text = record.getMessage()
            if record.exc_info and record.exc_info[0] is not None:
                text = f"{text} ({record.exc_info[0].__name__}: {record.exc_info[1]})"
            self.lines.append({"ts": record.created, "level": record.levelname, "text": text})
            if record.name == "relay" and self.store is not None:
                event, fields = parse_kv(record.getMessage())
                self.store.record(event, fields, record.created)
        except Exception:
            self.handleError(record)
