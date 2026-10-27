"""
Disk queue for mail Microsoft 365 could not take right away.

Each message is two files in QUEUE_DIR: `<id>.eml` (the message exactly as it will be
sent) and `<id>.json` (who, where, how many tries). The .json is written last, so a
job only exists once its message is safely on disk. Failed jobs move to `failed/`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path

from logfmt import kv
from retry import DeliveryError, queue_delay

log = logging.getLogger("relay")

JOB_ID = re.compile(r"^\d+-[0-9a-f]{8}$")  # Job.new_id(); anything else never touches the filesystem


@dataclass
class Job:
    id: str
    mailbox: str
    rcpt_tos: list[str]
    client_ip: str
    user: str | None
    sid: str | None
    size: int
    first_seen: float = field(default_factory=time.time)
    attempts: int = 0
    next_attempt: float = 0.0
    last_error: str = ""

    @staticmethod
    def new_id() -> str:
        return f"{int(time.time())}-{secrets.token_hex(4)}"

    def log_fields(self) -> dict[str, object]:
        return {"id": self.id, "ip": self.client_ip, "user": self.user,
                "from": self.mailbox, "to": ",".join(self.rcpt_tos)}


def _write_atomic(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


class MailQueue:
    def __init__(self, directory: Path, max_age_hours: float, clock: Callable[[], float] = time.time):
        self.dir = directory
        self.failed_dir = directory / "failed"
        self.max_age = max_age_hours * 3600
        self._clock = clock
        self.wake = asyncio.Event()
        self.delivering: str | None = None  # the job being sent right now, which cannot be cancelled

    @classmethod
    def open(cls, directory: Path, max_age_hours: float) -> MailQueue | None:
        """The queue, or None (with a warning) if the directory is not writable."""
        try:
            (directory / "failed").mkdir(parents=True, exist_ok=True)
            probe = directory / ".write-test"
            probe.write_bytes(b"")
            probe.unlink()
        except OSError as error:
            log.warning(kv("queue disabled", dir=directory, error=error,
                           effect="temporary-failures-answer-451-instead-of-queueing"))
            return None
        queue = cls(directory, max_age_hours)
        queue.recover()
        return queue

    def _paths(self, job_id: str, base: Path | None = None) -> tuple[Path, Path]:
        base = base or self.dir
        return base / f"{job_id}.eml", base / f"{job_id}.json"

    def put(self, job: Job, mime: bytes) -> None:
        eml, meta = self._paths(job.id)
        _write_atomic(eml, mime)
        _write_atomic(meta, json.dumps(asdict(job)).encode())

    def jobs(self) -> list[Job]:
        jobs = []
        for meta in self.dir.glob("*.json"):
            try:
                jobs.append(Job(**json.loads(meta.read_text())))
            except (OSError, ValueError, TypeError) as error:
                log.error(kv("queue unreadable", file=meta.name, error=error))
                os.replace(meta, self.failed_dir / meta.name)
        return sorted(jobs, key=lambda j: j.first_seen)

    def read(self, job: Job) -> bytes:
        return self._paths(job.id)[0].read_bytes()

    def complete(self, job: Job) -> None:
        for path in self._paths(job.id):
            path.unlink(missing_ok=True)

    def reschedule(self, job: Job, error: DeliveryError) -> float:
        job.attempts += 1
        delay = queue_delay(job.attempts, error.retry_after)
        job.next_attempt = self._clock() + delay
        job.last_error = str(error)
        _write_atomic(self._paths(job.id)[1], json.dumps(asdict(job)).encode())
        return delay

    def fail(self, job: Job, reason: str) -> None:
        job.last_error = reason
        eml, meta = self._paths(job.id)
        failed_eml, failed_meta = self._paths(job.id, self.failed_dir)
        if eml.exists():
            os.replace(eml, failed_eml)
        _write_atomic(failed_meta, json.dumps(asdict(job)).encode())
        meta.unlink(missing_ok=True)

    def cancel(self, job_id: str, reason: str) -> tuple[str, Job | None]:
        """Stop retrying a queued message; it moves to failed/. Returns (status, job):
        "cancelled", "not-found" (unknown or already done), or "sending" (in flight right now)."""
        if not JOB_ID.match(job_id or ""):
            return "not-found", None
        if job_id == self.delivering:
            return "sending", None
        meta = self._paths(job_id)[1]
        try:
            job = Job(**json.loads(meta.read_text()))
        except (OSError, ValueError, TypeError):
            return "not-found", None
        self.fail(job, reason)
        return "cancelled", job

    def recover(self) -> None:
        """Startup tidy-up: leftover temp files, and messages whose .json never got written."""
        for tmp in self.dir.glob("*.tmp"):
            tmp.unlink(missing_ok=True)
        for eml in self.dir.glob("*.eml"):
            if not eml.with_suffix(".json").exists():
                # The client never got a 250 for this one, so it will have retried.
                log.warning(kv("queue orphan", file=eml.name, action="moved-to-failed"))
                os.replace(eml, self.failed_dir / eml.name)
        pending = len(list(self.dir.glob("*.json")))
        log.info(kv("queue ready", dir=self.dir, pending=pending))

    async def process_due(self, deliver: Callable[[Job, bytes], Awaitable[int]]) -> None:
        for job in self.jobs():
            now = self._clock()
            if job.next_attempt > now:
                continue
            if not self._paths(job.id)[1].exists():
                continue  # cancelled from the dashboard since the list was read
            if now - job.first_seen >= self.max_age:
                self.fail(job, f"expired after {job.attempts} tries: {job.last_error}")
                log.error(kv("queue failed", **job.log_fields(), attempts=job.attempts,
                             reason="expired", last_error=job.last_error))
                continue
            try:
                mime = self.read(job)
            except OSError as error:
                self.fail(job, f"message file unreadable: {error}")
                log.error(kv("queue failed", **job.log_fields(), reason="unreadable"))
                continue

            started = time.monotonic()
            self.delivering = job.id
            try:
                status = await deliver(job, mime)
            except DeliveryError as error:
                if error.transient:
                    delay = self.reschedule(job, error)
                    log.warning(kv("queue retry", **job.log_fields(), attempt=job.attempts,
                                   next_in=f"{delay / 60:.0f}m", reason=error))
                else:
                    self.fail(job, f"rejected: {error}")
                    log.error(kv("queue failed", **job.log_fields(), attempts=job.attempts + 1,
                                 reason="rejected", graph=error.status, error=error))
                continue
            except Exception as error:
                delay = self.reschedule(job, DeliveryError(f"unexpected: {error!r}", transient=True))
                log.exception(kv("queue retry", **job.log_fields(), attempt=job.attempts,
                                 next_in=f"{delay / 60:.0f}m", reason="unexpected-error"))
                continue
            finally:
                self.delivering = None

            self.complete(job)
            log.info(kv("queue sent", **job.log_fields(), attempts=job.attempts + 1, graph=status,
                        ms=int((time.monotonic() - started) * 1000)))

    async def run(self, deliver: Callable[[Job, bytes], Awaitable[int]], interval: float = 30.0) -> None:
        """Background worker: check the queue every `interval` seconds, or sooner when woken."""
        while True:
            self.wake.clear()
            try:
                await self.process_due(deliver)
            except Exception:
                log.exception("queue worker error")
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=interval)
            except TimeoutError:
                pass
