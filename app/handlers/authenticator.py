import hmac
import logging

from aiosmtpd.smtp import AuthResult, LoginPassword

from event_bus import event_bus_instance
from handlers.access import LoginLockout
from logfmt import kv, peer_ip, session_fields

log = logging.getLogger("relay")


class Authenticator:
    """
    Checks the SMTP login against SMTP_AUTH_USER / SMTP_AUTH_PASS.

    aiosmtpd calls this for AUTH PLAIN and AUTH LOGIN. Plaintext logins are allowed
    (older scanners cannot do STARTTLS) but logged as warnings with the device IP.
    """

    def __init__(self, username: str, password: str, lockout: LoginLockout | None = None):
        self._username = username.encode("utf-8")
        self._password = password.encode("utf-8")
        self.lockout = lockout or LoginLockout()

    def __call__(self, server, session, envelope, mechanism, auth_data):
        ip = peer_ip(session)
        fields = session_fields(session)
        fields["mech"] = mechanism

        if not isinstance(auth_data, LoginPassword):
            log.warning(kv("auth failed", **fields, reason="unsupported-mechanism"))
            return AuthResult(success=False, handled=False, message="504 5.5.4 Unrecognized authentication type")

        fields["user"] = auth_data.login.decode("utf-8", "replace")
        if self.lockout.is_locked(ip):
            log.warning(kv("auth locked", **fields))
            return AuthResult(
                success=False, handled=False, message="454 4.7.0 Too many failed logins, try again later"
            )

        try:
            event_bus_instance.publishSync("before_auth", auth_data)
        except Exception:
            log.exception(kv("auth error", **fields, reason="before_auth-middleware"))
            return AuthResult(success=False, handled=False, message="454 4.7.0 Temporary authentication failure")

        # Compare both parts, always, in constant time.
        user_ok = hmac.compare_digest(auth_data.login, self._username)
        pass_ok = hmac.compare_digest(auth_data.password, self._password)
        if not (user_ok and pass_ok):
            fails = self.lockout.record_failure(ip)
            log.warning(kv("auth failed", **fields, fails=fails))
            return AuthResult(success=False, handled=False, message="535 5.7.8 Authentication credentials invalid")

        self.lockout.record_success(ip)
        if fields["tls"] == "no":
            log.warning(kv("auth ok", **fields, note="plaintext-login-enable-STARTTLS-on-this-device"))
        else:
            log.info(kv("auth ok", **fields))
        try:
            event_bus_instance.publishSync("after_auth", auth_data)
        except Exception:
            log.exception(kv("auth error", **fields, reason="after_auth-middleware"))
        return AuthResult(success=True, auth_data=auth_data)
