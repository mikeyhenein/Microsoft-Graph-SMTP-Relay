# Changelog

Each release bumps the version in `app/version.py` and `pyproject.toml` (a test keeps them in step) and gets a
`vX.Y.Z` git tag.

## 2.2.2 (2026-10-25)

- Needs attention: a device's address line wraps between words instead of in the middle of an IP address.

## 2.2.1 (2026-10-25)

- Dashboard no longer flashes an empty layout (with "Send a test" near the top) on refresh: the cards stay
  hidden until the first data arrives, then appear once in their final place.

## 2.2.0 (2026-10-25)

- Cancel pending retries from the dashboard: a "Cancel" button on each retrying message and "Cancel all
  retries". Cancelled messages move to the queue's `failed/` folder (kept, not deleted) and the log records who
  cancelled them. A message that is being sent at that moment cannot be cancelled.

## 2.1.1 (2026-10-25)

- The dashboard's test email shows the relay version (in the subject, the body and an `X-Relay-Version` header).

## 2.1.0 (2026-10-25)

- Dashboard v2: problems first with one-line fixes, devices by name, activity and retries in one list,
  test send at the bottom with From suggestions, raw log folded away.
- TLS setup failures (e.g. a device rejecting the self-signed certificate) are one clear log line and a
  dashboard status instead of a traceback.
- Device names: DNS hostname lookup; apps that say hello as `[127.0.0.1]` show their DNS name instead.
- Dashboard history kept 30 days (was 7). Version shown on the sign-in page, dashboard and startup log.
- Dashboard published on host port 8025 in docker-compose.yml.

## 2.0.0 (2026-10-24)

First release of this fork: Python 3.14, security fixes (no open relay, sender allow-list, login lockout,
STARTTLS), raw-MIME Graph sending, retries with a disk queue, per-IP logging, web dashboard with test send,
and the Microsoft 365 setup script.
