import json
import time

from mail_queue import Job, MailQueue
from retry import DeliveryError


def make_job(**overrides) -> Job:
    fields = dict(id=Job.new_id(), mailbox="scanner@contoso.com", rcpt_tos=["boss@example.com"],
                  client_ip="192.168.1.40", user="relay", sid="abc123", size=5)
    fields.update(overrides)
    return Job(**fields)


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


def open_queue(tmp_path, clock=None, max_age_hours=24.0) -> MailQueue:
    queue = MailQueue.open(tmp_path / "queue", max_age_hours)
    if clock:
        queue._clock = clock
    return queue


def test_put_writes_message_then_metadata(tmp_path):
    queue = open_queue(tmp_path)
    job = make_job()
    queue.put(job, b"hello")
    assert queue.read(job) == b"hello"
    assert [j.id for j in queue.jobs()] == [job.id]
    assert not list((tmp_path / "queue").glob("*.tmp"))


def test_recover_moves_orphans_and_removes_temp_files(tmp_path):
    queue = open_queue(tmp_path)
    (tmp_path / "queue" / "123-orphan.eml").write_bytes(b"x")
    (tmp_path / "queue" / "half.json.tmp").write_bytes(b"x")
    queue.recover()
    assert (tmp_path / "queue" / "failed" / "123-orphan.eml").exists()
    assert not (tmp_path / "queue" / "half.json.tmp").exists()


async def test_due_job_is_sent_and_removed(tmp_path):
    clock = Clock(time.time())
    queue = open_queue(tmp_path, clock)
    job = make_job(next_attempt=clock.now - 1)
    queue.put(job, b"mime")
    delivered = []

    async def deliver(j, mime):
        delivered.append((j.id, mime))
        return 202

    await queue.process_due(deliver)
    assert delivered == [(job.id, b"mime")]
    assert queue.jobs() == []


async def test_not_yet_due_job_waits(tmp_path):
    clock = Clock(time.time())
    queue = open_queue(tmp_path, clock)
    queue.put(make_job(next_attempt=clock.now + 60), b"mime")

    async def deliver(j, mime):
        raise AssertionError("should not be called yet")

    await queue.process_due(deliver)
    assert len(queue.jobs()) == 1


async def test_transient_failure_reschedules_with_backoff(tmp_path):
    clock = Clock(time.time())
    queue = open_queue(tmp_path, clock)
    job = make_job()
    queue.put(job, b"mime")

    async def deliver(j, mime):
        raise DeliveryError("POST 503", transient=True, status=503)

    await queue.process_due(deliver)
    [saved] = queue.jobs()
    assert saved.attempts == 1
    assert saved.next_attempt == clock.now + 60
    assert "503" in saved.last_error


async def test_permanent_failure_moves_to_failed(tmp_path):
    queue = open_queue(tmp_path)
    job = make_job()
    queue.put(job, b"mime")

    async def deliver(j, mime):
        raise DeliveryError("POST 403 ErrorSendAsDenied", transient=False, status=403)

    await queue.process_due(deliver)
    assert queue.jobs() == []
    failed = json.loads((tmp_path / "queue" / "failed" / f"{job.id}.json").read_text())
    assert failed["last_error"].startswith("rejected")
    assert (tmp_path / "queue" / "failed" / f"{job.id}.eml").read_bytes() == b"mime"


async def test_expired_job_moves_to_failed_without_another_try(tmp_path):
    clock = Clock(time.time())
    queue = open_queue(tmp_path, clock, max_age_hours=1)
    job = make_job(first_seen=clock.now - 3601)
    queue.put(job, b"mime")

    async def deliver(j, mime):
        raise AssertionError("expired jobs are not retried")

    await queue.process_due(deliver)
    assert queue.jobs() == []
    assert (tmp_path / "queue" / "failed" / f"{job.id}.json").exists()


def test_cancel_moves_the_job_to_failed(tmp_path):
    queue = open_queue(tmp_path)
    job = make_job()
    queue.put(job, b"mime")
    status, cancelled = queue.cancel(job.id, "cancelled from the dashboard by admin")
    assert status == "cancelled" and cancelled.id == job.id
    assert queue.jobs() == []
    failed = json.loads((tmp_path / "queue" / "failed" / f"{job.id}.json").read_text())
    assert failed["last_error"] == "cancelled from the dashboard by admin"
    assert (tmp_path / "queue" / "failed" / f"{job.id}.eml").read_bytes() == b"mime"
    assert queue.cancel(job.id, "again")[0] == "not-found"


def test_cancel_rejects_ids_that_are_not_job_ids(tmp_path):
    queue = open_queue(tmp_path)
    (tmp_path / "secret.json").write_text("{}")
    for bad in ("../secret", "..\\secret", "", "1792950485-F8926296", "1792950485-f8926296/../x"):
        assert queue.cancel(bad, "x") == ("not-found", None)
    assert (tmp_path / "secret.json").exists()


async def test_a_message_being_sent_cannot_be_cancelled(tmp_path):
    clock = Clock(time.time())
    queue = open_queue(tmp_path, clock)
    job = make_job(next_attempt=0)
    queue.put(job, b"mime")
    seen = {}

    async def deliver(j, mime):
        seen["status"] = queue.cancel(j.id, "too late")[0]
        return 202

    await queue.process_due(deliver)
    assert seen["status"] == "sending"
    assert queue.delivering is None
    assert not (tmp_path / "queue" / "failed" / f"{job.id}.json").exists()


async def test_job_cancelled_while_another_is_sending_is_skipped(tmp_path):
    clock = Clock(time.time())
    queue = open_queue(tmp_path, clock)
    first = make_job(first_seen=clock.now - 20, next_attempt=0)
    second = make_job(first_seen=clock.now - 10, next_attempt=0)
    queue.put(first, b"one")
    queue.put(second, b"two")
    delivered = []

    async def deliver(j, mime):
        delivered.append(j.id)
        if j.id == first.id:
            assert queue.cancel(second.id, "cancelled meanwhile")[0] == "cancelled"
        return 202

    await queue.process_due(deliver)
    assert delivered == [first.id]
    failed = json.loads((tmp_path / "queue" / "failed" / f"{second.id}.json").read_text())
    assert failed["last_error"] == "cancelled meanwhile"  # not overwritten as "unreadable"


def test_unwritable_directory_disables_the_queue(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("not a directory")
    assert MailQueue.open(blocker / "queue", 24) is None
