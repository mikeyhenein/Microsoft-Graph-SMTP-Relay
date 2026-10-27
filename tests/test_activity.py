import logging

from activity import ActivityLogHandler, ActivityStore, parse_kv
from logfmt import kv


def test_parse_kv_round_trips_the_relay_log_format():
    line = kv("auth ok", sid="ab12", ip="192.168.1.40", user="relay", tls="no", note=None,
              reason='POST 503 "busy" now', settings="listen=0.0.0.0:2525 user=relay")
    event, fields = parse_kv(line)
    assert event == "auth ok"
    assert fields == {"sid": "ab12", "ip": "192.168.1.40", "user": "relay", "tls": "no", "note": None,
                      "reason": 'POST 503 "busy" now', "settings": "listen=0.0.0.0:2525 user=relay"}


def test_parse_kv_plain_text():
    assert parse_kv("relay stopped") == ("relay stopped", {})


class Clock:
    def __init__(self, now=1_800_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


def feed(store, clock, *lines):
    for line in lines:
        event, fields = parse_kv(line)
        store.record(event, fields, clock.now)


def test_store_builds_devices_messages_and_stats(tmp_path):
    clock = Clock()
    store = ActivityStore(tmp_path / "activity.db", clock=clock)
    assert store.persistent
    feed(store, clock,
         kv("connect", sid="s1", ip="192.168.1.40", port=5000),
         kv("helo", sid="s1", ip="192.168.1.40", name="SCANNER-2F"),
         kv("auth ok", sid="s1", ip="192.168.1.40", user="relay", tls="no"),
         kv("message", sid="s1", ip="192.168.1.40", user="relay", tls="no", **{"from": "scanner@contoso.com"},
            to="ap@contoso.com", size=2048, result="sent", graph=202, attempts=1),
         kv("message", sid="s2", ip="10.0.0.5", user="relay", tls="yes", **{"from": "alerts@contoso.com"},
            to="ops@contoso.com", size=10, result="queued", graph=503, attempts=3, queue_id="q1"),
         kv("reject", sid="s3", ip="192.168.1.40", user="relay", cmd="MAIL", code=550,
            reason="sender-not-allowed", address="ceo@contoso.com"),
         kv("auth failed", sid="s4", ip="192.168.1.99", user="admin", mech="PLAIN", fails=5),
         kv("auth locked", sid="s4", ip="192.168.1.99", user="admin"))
    clock.now += 60
    feed(store, clock, kv("queue sent", id="q1", ip="10.0.0.5", attempts=1, graph=202))

    summary = store.summary()
    assert summary["stats"]["sent"] == 2
    assert summary["stats"]["retried"] == 1
    assert summary["stats"]["rejected"] == 1 and summary["stats"]["rejected_by_relay"] == 1
    assert summary["stats"]["devices"] == 3 and summary["stats"]["devices_without_tls"] == 1

    devices = {d["ip"]: d for d in summary["devices"]}
    scanner = devices["192.168.1.40"]
    assert scanner["name"] == "SCANNER-2F" and scanner["tls"] == "no" and scanner["messages_24h"] == 2
    assert scanner["last_result"] == "Sender not allowed" and scanner["last_kind"] == "rejected"
    assert devices["192.168.1.99"]["last_result"].startswith("Locked out")

    queued = next(m for m in summary["messages"] if m["queue_id"] == "q1")
    assert queued["result"] == "sent" and queued["attempts"] == 4
    store.close()

    # History survives a restart
    reopened = ActivityStore(tmp_path / "activity.db", clock=clock)
    assert reopened.summary()["stats"]["sent"] == 2
    reopened.close()


def test_history_file_from_the_first_release_is_upgraded(tmp_path):
    import sqlite3
    old = sqlite3.connect(tmp_path / "activity.db")
    old.executescript(
        "CREATE TABLE messages (id INTEGER PRIMARY KEY, ts REAL NOT NULL, sid TEXT, ip TEXT, user TEXT, sender TEXT,"
        " rcpts TEXT, size INTEGER, tls TEXT, result TEXT, graph TEXT, attempts INTEGER, queue_id TEXT, reason TEXT);"
        "CREATE TABLE devices (ip TEXT PRIMARY KEY, name TEXT, first_seen REAL, last_seen REAL, tls TEXT,"
        " last_result TEXT, last_kind TEXT);"
        "INSERT INTO messages (ts, ip, sender, result) VALUES (1800000000, '10.0.0.1', 'a@b.com', 'sent');"
    )
    old.commit()
    old.close()
    clock = Clock()
    store = ActivityStore(tmp_path / "activity.db", clock=clock)
    feed(store, clock, kv("tls failed", ip="10.0.0.2", name="ninja", reason="untrusted-certificate"),
         kv("message", ip="10.0.0.1", name="SCANNER", result="sent", **{"from": "a@b.com"}, to="c@d.com"))
    summary = store.summary()
    assert summary["stats"]["sent"] == 2
    assert {m["name"] for m in summary["messages"]} == {None, "SCANNER"}
    assert any(d["last_reason"] == "tls-untrusted-certificate" for d in summary["devices"])
    store.close()


def test_old_history_is_pruned(tmp_path):
    clock = Clock()
    store = ActivityStore(tmp_path / "activity.db", clock=clock)
    feed(store, clock, kv("message", ip="1.2.3.4", result="sent", **{"from": "a@b.com"}, to="c@d.com"))
    clock.now += 29 * 86400
    store.prune()
    assert len(store.summary()["messages"]) == 1  # kept for 30 days
    clock.now += 2 * 86400
    store.prune()
    assert store.summary()["messages"] == []
    store.close()


def test_unwritable_path_falls_back_to_memory(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    store = ActivityStore(blocker / "activity.db")
    assert not store.persistent
    store.close()


def test_log_handler_keeps_relay_lines_and_other_warnings():
    handler = ActivityLogHandler(None, max_lines=3)
    for name, level, text in (("relay", logging.INFO, "a"), ("somelib", logging.INFO, "noise"),
                              ("somelib", logging.WARNING, "b"), ("relay", logging.INFO, "c"),
                              ("relay", logging.ERROR, "d")):
        handler.handle(logging.LogRecord(name, level, __file__, 1, text, (), None))
    # Other libraries' INFO lines are dropped; the buffer keeps the newest max_lines entries.
    assert [line["text"] for line in handler.lines] == ["b", "c", "d"]
    assert [line["level"] for line in handler.lines] == ["WARNING", "INFO", "ERROR"]
