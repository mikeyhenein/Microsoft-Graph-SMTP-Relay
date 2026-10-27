import pytest

from handlers.access import LoginLockout, normalize_mailbox, sender_allowed, valid_recipient


@pytest.mark.parametrize("address, expected", [
    ("Scanner@Contoso.com", "scanner@contoso.com"),
    ("first.last+tag@sub.contoso.com", "first.last+tag@sub.contoso.com"),
    ("o'brien@contoso.com", "o'brien@contoso.com"),
    ("", None),
    (None, None),
    ("a/../b@contoso.com", None),
    ("users%2fother@contoso.com", None),
    ("a?b@contoso.com", None),
    ("a#b@contoso.com", None),
    ("a\\b@contoso.com", None),
    (".lead@contoso.com", None),
    ("dots..double@contoso.com", None),
    ("nodomain@localhost", None),
    ("two@at@contoso.com", None),
])
def test_only_url_safe_mailboxes_pass(address, expected):
    assert normalize_mailbox(address) == expected


def test_recipients_are_checked_loosely():
    assert valid_recipient("someone@example.com")
    assert not valid_recipient("no-at-sign")
    assert not valid_recipient("a@b, c@d")
    assert not valid_recipient("<a@b>")


def test_sender_allowed_exact_and_domain():
    patterns = ("scanner@contoso.com", "*@lulu.test")
    assert sender_allowed("SCANNER@contoso.com", patterns)
    assert sender_allowed("anyone@lulu.test", patterns)
    assert not sender_allowed("ceo@contoso.com", patterns)
    assert not sender_allowed("x@evil.lulu.test", patterns)
    assert not sender_allowed("x@lulu.test.evil.com", patterns)


def test_lockout_after_five_failures_then_expires():
    now = [1000.0]
    lockout = LoginLockout(max_failures=5, window=900, lockout=900, clock=lambda: now[0])
    for expected in range(1, 5):
        assert lockout.record_failure("10.0.0.9") == expected
        assert not lockout.is_locked("10.0.0.9")
    lockout.record_failure("10.0.0.9")
    assert lockout.is_locked("10.0.0.9")
    assert not lockout.is_locked("10.0.0.10")  # per IP
    now[0] += 901
    assert not lockout.is_locked("10.0.0.9")


def test_success_clears_failures_and_old_failures_age_out():
    now = [0.0]
    lockout = LoginLockout(max_failures=3, window=60, clock=lambda: now[0])
    lockout.record_failure("ip")
    lockout.record_failure("ip")
    lockout.record_success("ip")
    assert lockout.record_failure("ip") == 1
    now[0] += 61
    assert lockout.record_failure("ip") == 1
