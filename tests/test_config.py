import pytest

from config import SMALL_MESSAGE_LIMIT, ConfigError, load_settings

BASE = {
    "AUTHORITY": "https://login.microsoftonline.com/tenant",
    "CLIENT_ID": "client",
    "CLIENT_SECRET": "secret",
    "SMTP_AUTH_USER": "relay",
    "SMTP_AUTH_PASS": "a-long-enough-password",
    "SMTP_ALLOWED_SENDERS": "Scanner@Contoso.com, *@contoso.com",
}


def problems(env) -> list[str]:
    with pytest.raises(ConfigError) as caught:
        load_settings(env)
    return caught.value.problems


def test_minimal_environment_uses_defaults():
    s = load_settings(BASE)
    assert s.port == 2525
    assert s.allowed_senders == ("scanner@contoso.com", "*@contoso.com")
    assert s.large_messages is False
    assert s.max_message_size == SMALL_MESSAGE_LIMIT
    assert str(s.queue_dir).replace("\\", "/") == "/data/queue"
    assert s.warnings == ()


def test_secrets_stay_out_of_repr_and_summary():
    s = load_settings(BASE)
    assert "secret" not in repr(s).replace("client_secret", "")
    assert "a-long-enough-password" not in repr(s)
    assert "a-long-enough-password" not in s.summary()


def test_running_without_a_login_is_refused():
    env = {k: v for k, v in BASE.items() if k != "SMTP_AUTH_PASS"}
    assert any("open relay" in p for p in problems(env))


def test_every_problem_is_reported_together():
    found = problems({"SMTP_RELAY_PORT": "99999", "LARGE_MESSAGES": "maybe"})
    joined = "\n".join(found)
    for name in ("AUTHORITY", "CLIENT_ID", "CLIENT_SECRET", "SMTP_AUTH_USER", "SMTP_ALLOWED_SENDERS",
                 "SMTP_RELAY_PORT", "LARGE_MESSAGES"):
        assert name in joined


def test_allowed_senders_must_be_addresses_or_domains():
    found = problems({**BASE, "SMTP_ALLOWED_SENDERS": "scanner@contoso.com, *, a/../b@contoso.com"})
    assert sum("SMTP_ALLOWED_SENDERS entry" in p for p in found) == 2


def test_tls_files_come_in_pairs():
    assert any("TLS_CERT_FILE" in p for p in problems({**BASE, "TLS_CERT_FILE": "/x/cert.pem"}))


def test_big_messages_need_the_large_path():
    assert any("LARGE_MESSAGES" in p for p in problems({**BASE, "MAX_MESSAGE_SIZE": str(SMALL_MESSAGE_LIMIT + 1)}))
    s = load_settings({**BASE, "LARGE_MESSAGES": "true"})
    assert s.max_message_size == 35 * 1024 * 1024


def test_short_password_and_retired_setting_are_warnings():
    s = load_settings({**BASE, "SMTP_AUTH_PASS": "short-pass1", "SAVE_TO_SENT": "false"})
    assert len(s.warnings) == 2
    assert any("16" in w for w in s.warnings)
    assert any("SAVE_TO_SENT" in w for w in s.warnings)


def test_dashboard_settings():
    assert not load_settings(BASE).dashboard_enabled
    s = load_settings({**BASE, "DASHBOARD_USER": "admin", "DASHBOARD_PASS": "a-separate-long-password"})
    assert s.dashboard_enabled and s.dashboard_port == 8080 and s.dashboard_https is True
    assert "a-separate-long-password" not in repr(s)
    assert "dashboard=0.0.0.0:8080" in s.summary()

    assert any("DASHBOARD_PASS" in p for p in problems({**BASE, "DASHBOARD_USER": "admin"}))
    assert any("DASHBOARD_PORT" in p for p in problems(
        {**BASE, "DASHBOARD_USER": "admin", "DASHBOARD_PASS": "a-separate-long-password", "DASHBOARD_PORT": "2525"}))
    reused = load_settings({**BASE, "DASHBOARD_USER": "admin", "DASHBOARD_PASS": BASE["SMTP_AUTH_PASS"]})
    assert any("same as SMTP_AUTH_PASS" in w for w in reused.warnings)


def test_relative_middleware_dir_resolves_next_to_the_code():
    s = load_settings({**BASE, "MIDDLEWARE_DIR": "middleware"})
    assert s.middleware_dir.is_absolute()
    assert s.middleware_dir.name == "middleware"
