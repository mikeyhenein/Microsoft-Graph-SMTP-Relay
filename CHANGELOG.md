# Changelog

Each release bumps the version in `app/version.py` and `pyproject.toml` (a test keeps them in step) and gets a
`vX.Y.Z` git tag.

## 2.3.0 (2026-11-18)

Security fixes from a red-team review. Devices keep their settings; see "Upgrading" below.

### Security

- One unauthenticated connection could flood EHLO commands and stall all mail. Commands per connection are
  limited, a client may send 4 KB before logging in, and connections are capped per IP and in total.
- A device name or login containing `\"` could forge devices, login failures and TLS problems on the dashboard.
  The dashboard now reads log fields directly; log values escape backslashes and drop control characters
  (including terminal escape codes).
- A message with a very large To/Cc header froze the relay for minutes. Header lines are capped at 64 KB and
  address parsing runs in a worker thread.
- The From check could be sidestepped with `From : x` (space before the colon) or a malformed first header line.
  Malformed header lines are refused; From and Sender must each be exactly the sender address.
- Five wrong passwords locked an IP out, right password included (on one Docker host, every app shares an IP).
  Failed logins now slow that IP to one checked attempt every 10 seconds; the right password still gets in.
  Guessing from many IPs slows every IP that has never logged in.
- The `.env.sample` placeholder passwords were accepted. The relay now refuses to start with them, and the sample
  ships with the dashboard switched off.
- Recipients named only in the message headers now show in Activity. At most 500 recipients per message.
- Unexpected errors no longer send exception text to the device; broken messages get a permanent `554`.
- The retry queue is capped (2,000 messages or 1 GB) and `queue/failed/` is emptied after 30 days. The
  history database is capped at 200,000 messages and 5,000 devices.
- A dashboard cancel could race with a send in progress; it no longer can.
- Dashboard: test sends limited to 5 a minute; posts from other sites refused; no version before sign-in or in
  the `Server` header; 404/405 carry the security headers; signing out ends every session. The SMTP greeting no
  longer names the product.
- `docker-compose.yml`: memory and process limits, and log rotation.
- `tools/Setup-RelayTenant.ps1`: an existing app with the relay's name is shown with its owners and only used
  if you confirm; step 6 offers to remove mail-role assignments to the app outside the relay's scope.

### Upgrading

- `git pull`, then `docker compose up -d --build`. Recreating the container is what applies the new memory,
  process and log limits.
- If `SMTP_AUTH_PASS` or `DASHBOARD_PASS` still starts with `change-me`, set a real one first or the relay will
  not start.
- Things that are now refused: messages with malformed header lines, a From or Sender line that is not the
  sender, over 500 recipients, over 64 KB of header lines. The dashboard shows each with its fix.

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
