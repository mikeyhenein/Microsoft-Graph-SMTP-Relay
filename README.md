# Microsoft Graph SMTP Relay

An SMTP relay for devices and apps that can only send mail with a username and password (scanners,
printers, NAS boxes, older software). They log in to this relay; the relay sends each message as a
Microsoft 365 mailbox through the Microsoft Graph API, so Microsoft 365 never needs basic SMTP auth.

This is a maintained fork of [ggpwnkthx/microsoft-graph-smtp-relay](https://github.com/ggpwnkthx/microsoft-graph-smtp-relay),
updated for Python 3.14 with the security issues fixed (see [Changes from upstream](#changes-from-upstream)).

## How it works

1. A device connects on port 2525, optionally upgrades to TLS with STARTTLS, and logs in.
2. The relay checks the sender: it must be on `SMTP_ALLOWED_SENDERS` and match the message's From header.
3. Messages up to ~3 MB go to Graph `sendMail` exactly as the device built them (needs only `Mail.Send`).
   Larger messages are sent as a draft with chunked attachment uploads, if you turn on `LARGE_MESSAGES`.
4. If Microsoft is briefly unavailable or throttling, the relay retries for up to 30 seconds. If it still
   fails, the message goes to a queue on disk and the device is told it was accepted; the queue keeps retrying
   for up to 24 hours, across restarts.
5. A copy of each message lands in the sending mailbox's Sent Items.

## Quick start (Docker Compose)

```bash
cp .env.sample .env
```

Fill in `.env` (see [Configuration](#configuration)), then:

```bash
docker compose up -d --build
```

```bash
docker compose logs -f smtp-relay
```

Point devices at the Docker host on port **2525**, with the `SMTP_AUTH_USER` / `SMTP_AUTH_PASS` login.
Each device's configured sender ("From") address must be listed in `SMTP_ALLOWED_SENDERS`.
**Setting up a device or app (host, encryption, Verify peer, Local Domain / EHLO name, OMV and Invoice Ninja
examples): [docs/connecting-devices.md](docs/connecting-devices.md).**
If a device can only use port 25 or 587, change the port mapping in `docker-compose.yml` to `"25:2525"`
or `"587:2525"`.

Run it on a Linux Docker host with the default bridge network so the logs show each device's real IP.
(Docker Desktop on Windows/macOS shows its VM gateway address instead.)

**Apps on the same host as the relay: use the host's LAN IP, not `localhost` / `127.0.0.1`.**

- **OpenMediaVault notifications:** with `localhost` or `127.0.0.1` as the SMTP server, OMV leaves Postfix's
  relayhost empty and tries to deliver straight to the recipient's mail server on port 25 (usually blocked by the
  ISP), so nothing reaches the relay. Use the host's LAN IP, port 2525.
- **Apps in their own container (e.g. Invoice Ninja):** `127.0.0.1` inside a container is that container, not the
  host, so it never reaches the relay. Use the host's LAN IP, or `smtp-relay` on a shared Docker network (below).

Two cases where you see a Docker address (`172.x.0.1`) instead of the device:

- **The sender is another container on the same host** (for example Invoice Ninja) and connects to the host's IP.
  Docker rewrites that traffic to its gateway address, which DNS names after the host. Two ways to see the app instead:
  - Quickest: set the app's EHLO / "Local Domain" setting to a name (e.g. `invoiceninja`). The dashboard shows it.
  - Automatic: put the app on the relay's Docker network and point it at host `smtp-relay`, port 2525. The relay
    then sees the container's own address and Docker's DNS gives its container name (e.g. `invoiceninja-app-1`).
    In the app's compose file, join the relay's network (the network name is `<relay folder>_default`):

    ```yaml
    services:
      app:
        networks: [default, relay]
    networks:
      relay:
        external: true
        name: smtp-graph-relay_default
    ```
- **Every device shows the same 172.x address**, LAN devices included: that host's Docker is not passing client
  addresses through (Docker Desktop, rootless Docker, or the userland proxy). Run the relay with
  `network_mode: host` (Linux) so it listens on the host directly; remove the `ports:` mapping when you do.

The relay also looks up each device's name in DNS (shown under the device on the dashboard and as `host=` in the
log), so devices your router registers in DNS show a name as well as an IP.

## Microsoft 365 setup

Create the Entra app registration and, strongly recommended, restrict it to the mailboxes the relay
sends from. Without that restriction the app's `Mail.Send` permission can send as **any** mailbox in the tenant.

The guided PowerShell script does both and writes the IDs and secret into `.env` (PowerShell 7):

```powershell
pwsh ./tools/Setup-RelayTenant.ps1
```

The manual steps and background are in [docs/tenant-setup.md](docs/tenant-setup.md).

## Dashboard

Set `DASHBOARD_USER` and `DASHBOARD_PASS` in `.env` (use a different password from the SMTP one) and open
`https://<docker-host>:8025` (the compose file publishes the dashboard's port 8080 on host port 8025, since 8080 is
often taken). It shows:

- **Overview** for the last 24 hours: sent, waiting in the retry queue, rejected, and devices seen.
- **Devices by source IP**: HELO name, last seen, message count, whether they use STARTTLS, and their last result
  (including lockouts).
- **Recent messages**, the **retry queue** with each message's next try and last error, and the **live log**
  with Warnings/Errors filters and a search box.
- **Send a test**: type any From and To address and send a short message through the relay. It logs in like a
  device does, so it exercises the same checks, retries and queue. The From address still has to be on
  `SMTP_ALLOWED_SENDERS`; if it is not, the result says so.

The dashboard uses the relay's certificate for HTTPS. The default one is self-signed, so the browser warns once.
Behind a reverse proxy, set `DASHBOARD_HTTPS=false` and let the proxy handle TLS. Sign-ins are rate limited
(5 failures locks the IP out for 15 minutes) and sessions end after 8 hours idle. History is kept for 30 days in
`$DATA_DIR/activity.db`. With the dashboard off (no `DASHBOARD_USER`/`DASHBOARD_PASS`), its port is not opened.

The version is shown on the sign-in page, the dashboard footer and the `relay started` log line; see
[CHANGELOG.md](CHANGELOG.md).

## Configuration

All settings are environment variables, read from `.env` by Compose. Keep `.env` out of git (it is
ignored) and keep comments on their own lines (`docker --env-file` does not strip trailing comments).

| Variable | Default | Notes |
|---|---|---|
| `AUTHORITY` | required | `https://login.microsoftonline.com/<directory (tenant) id>` |
| `CLIENT_ID` | required | Application (client) ID of the Entra app |
| `CLIENT_SECRET` | required | Client secret value. Rotate it before it expires (Entra shows the date) |
| `SMTP_AUTH_USER` / `SMTP_AUTH_PASS` | required | The login every device uses. Use 16+ characters |
| `SMTP_ALLOWED_SENDERS` | required | Comma-separated From addresses the login may use: `scanner@contoso.com,*@contoso.com` |
| `SMTP_RELAY_HOSTNAME` / `SMTP_RELAY_PORT` | `0.0.0.0` / `2525` | Listener inside the container |
| `DATA_DIR` | `/data` | TLS certificate, queue and log file |
| `TLS_CERT_FILE` / `TLS_KEY_FILE` | generated | Your own STARTTLS certificate. Unset: a self-signed one is created once in `$DATA_DIR/tls` |
| `LARGE_MESSAGES` | `false` | Allow messages over ~3 MB (draft + upload path, needs `Mail.ReadWrite`) |
| `MAX_MESSAGE_SIZE` | 3,000,000 / 36,700,160 | Bytes. The second default applies when `LARGE_MESSAGES=true`; Exchange's own limit is 35 MB by default |
| `QUEUE_DIR` | `$DATA_DIR/queue` | Retry queue |
| `QUEUE_MAX_AGE_HOURS` | `24` | How long a queued message keeps being retried |
| `MIDDLEWARE_DIR` | `<app>/middleware` | Python hooks, see [MIDDLEWARE.md](MIDDLEWARE.md) |
| `LOG_LEVEL` | `INFO` | `DEBUG` also shows every SMTP command |
| `LOG_FILE_ENABLED` / `LOG_FILE` | `false` / `$DATA_DIR/logs/smtp_relay.log` | Rotating file, in addition to stdout |
| `DASHBOARD_USER` / `DASHBOARD_PASS` | off | Dashboard sign-in. Both set turns the dashboard on |
| `DASHBOARD_HOST` / `DASHBOARD_PORT` | `0.0.0.0` / `8080` | Dashboard listener |
| `DASHBOARD_HTTPS` | `true` | Serve the dashboard over HTTPS with the relay's certificate |

## Logs: who is sending what

Every line is `event key=value ...`, so it greps well:

```text
connect sid=7f3a21 ip=192.168.1.40 port=51234
helo sid=7f3a21 ip=192.168.1.40 user=- tls=no name=SCANNER-2F
auth ok sid=7f3a21 ip=192.168.1.40 user=relay tls=no mech=LOGIN note=plaintext-login-enable-STARTTLS-on-this-device
message sid=7f3a21 ip=192.168.1.40 user=relay tls=no from=scanner@contoso.com to=ap@contoso.com size=2203114 result=sent graph=202 attempts=1 ms=1840
disconnect sid=7f3a21 ip=192.168.1.40
```

- `sid` ties together the lines of one connection; `ip` is the device.
- `result` is `sent`, `queued` (with `queue_id`), `rejected`, `deferred` (device told to retry) or `skipped` (middleware).
- A plaintext login (`tls=no`) is a WARNING: the password crossed the network unencrypted. Turn on TLS/STARTTLS
  in that device's settings if it supports it.
- `tls failed ... reason=...`: the device could not set up encryption, so it never logged in. The dashboard shows
  the same problem on the device's row with the fix. `untrusted-certificate` means the device verifies certificates
  and rejects the relay's self-signed one (turn off "verify peer/certificate" on the device, or set `TLS_CERT_FILE`);
  `implicit-tls` means it is set to SSL/TLS instead of STARTTLS; `old-tls-version` means it only speaks TLS 1.0/1.1.
- `auth failed` / `auth locked`: wrong password; after 5 failures in 15 minutes an IP is locked out for 15 minutes.
- `reject ... reason=sender-not-allowed`: the device tried a From address not in `SMTP_ALLOWED_SENDERS`.
- Queue lines: `queue retry`, `queue sent`, `queue failed` (ERROR: gave up; the message is kept in `queue/failed/`).

Find every device that sent mail today:

```bash
docker compose logs --since 24h smtp-relay | grep -o 'message sid=[^ ]* ip=[^ ]*' | awk '{print $3}' | sort | uniq -c
```

## Retries and the queue

| Graph says | Relay does |
|---|---|
| 202 Accepted | `250` to the device |
| 400 / 403 / 404 / 413 (message or mailbox refused) | `5xx` to the device, not retried |
| 429 / 5xx / timeout / token error | Retry at ~1s and ~4s (honouring `Retry-After`), then queue to disk and answer `250 Queued as <id>` |

The queue retries after 1, 5, 15 and 30 minutes, then hourly, until `QUEUE_MAX_AGE_HOURS`. Queued messages are
stored on the `/data` volume until they are delivered or moved to `queue/failed/`, so treat that volume as
containing mail. If the queue directory is not writable, temporary failures answer `451` and the device retries.

## Development

Tests run against a fake Graph server, no Microsoft account needed:

```bash
docker run --rm -v "$PWD:/w" -w /w python:3.14-slim sh -c "pip install -q --require-hashes -r requirements.txt -r requirements-dev.txt && ruff check app tests && pytest -q"
```

Send a real test message through a running relay (uses `.env`; sends real mail):

```bash
python tools/send_test.py --to you@contoso.com --starttls
```

Dependencies are pinned with hashes. To update them, edit `requirements.in` / `requirements-dev.in` and run:

```bash
uv pip compile requirements.in --generate-hashes --python-version 3.14 -o requirements.txt
```

The source is in `app/`: `main.py` (entry), `smtp_server.py` (listener, logging, lifecycle),
`handlers/` (login, sender checks, Graph client), `mail_queue.py`, `retry.py`, `config.py`.

## Changes from upstream

- **Security**
  - It can no longer run as an open relay.
  - Senders are limited to an allow-list and must match the From header.
  - Addresses are validated before they reach a Graph URL.
  - Constant-time password checks and a brute-force lockout; CRAM-MD5 is removed.
  - STARTTLS is offered.
  - Secrets live in `.env`, not compose.
  - The container runs as non-root on a read-only filesystem.
- **Reliability**
  - Failed sends are no longer reported as delivered.
  - Retries and a disk queue.
  - Attachments under 3 MB no longer go through upload sessions, which Graph rejects at that size.
- **Python 3.14**
  - The upstream code did not start on Python 3.14 (`asyncio.get_event_loop()`).
- **Permissions**
  - Only `Mail.Send` is needed unless large messages are enabled.
- **Behaviour changes**
  - Port 2525 inside the container.
  - A login and `SMTP_ALLOWED_SENDERS` are required.
  - `SAVE_TO_SENT` is gone; copies are always kept in Sent Items.

## License

MIT, see [LICENSE](LICENSE).
