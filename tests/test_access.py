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
    patterns = ("scanner@contoso.com", "*@home.test")
    assert sender_allowed("SCANNER@contoso.com", patterns)
    assert sender_allowed("anyone@home.test", patterns)
    assert not sender_allowed("ceo@contoso.com", patterns)
    assert not sender_allowed("x@evil.home.test", patterns)
    assert not sender_allowed("x@home.test.evil.example", patterns)


def test_five_failures_slow_an_address_down_without_locking_it_out():
    now = [1000.0]
    lockout = LoginLockout(max_failures=5, window=900, slow=10, clock=lambda: now[0])
    for expected in range(1, 5):
        assert lockout.allow("10.0.0.9")
        assert lockout.record_failure("10.0.0.9") == expected
    assert lockout.allow("10.0.0.9")
    lockout.record_failure("10.0.0.9")
    assert lockout.throttled("10.0.0.9") and not lockout.throttled("10.0.0.10")  # per address
    assert lockout.allow("10.0.0.9")  # one checked attempt...
    lockout.record_failure("10.0.0.9")
    assert not lockout.allow("10.0.0.9")  # ...then nothing for 10 seconds
    now[0] += 10
    assert lockout.allow("10.0.0.9")  # the right password gets in on this one
    lockout.record_success("10.0.0.9")
    assert not lockout.throttled("10.0.0.9") and lockout.allow("10.0.0.9")


def test_guessing_held_to_one_try_per_interval():
    now = [0.0]
    lockout = LoginLockout(max_failures=5, slow=10, clock=lambda: now[0])
    checked = 0
    for _ in range(600):  # one guess every 0.1 s for a minute
        if lockout.allow("ip"):
            checked += 1
            lockout.record_failure("ip")
        now[0] += 0.1
    assert checked <= 5 + 6 + 1


def test_guesses_spread_over_many_addresses_slow_every_untrusted_address():
    now = [0.0]
    lockout = LoginLockout(max_failures=5, storm=30, slow=10, clock=lambda: now[0])
    lockout.record_success("10.0.0.40")  # the scanner has logged in before
    for i in range(31):
        lockout.record_failure(f"10.9.0.{i}")
    assert lockout.throttled("10.9.9.9")  # a fresh address is slowed during the storm
    assert not lockout.throttled("10.0.0.40")
    now[0] += 61
    assert not lockout.throttled("10.9.9.9")


def test_success_clears_failures_and_old_failures_age_out():
    now = [0.0]
    lockout = LoginLockout(max_failures=3, window=60, clock=lambda: now[0])
    lockout.record_failure("ip")
    lockout.record_failure("ip")
    lockout.record_success("ip")
    assert lockout.record_failure("ip") == 1
    now[0] += 61
    assert lockout.record_failure("ip") == 1
