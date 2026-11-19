# Connecting devices and apps

How to set up a scanner, printer, NAS or app to send mail through the relay. The relay's own settings (the
`.env` file) are in the README's [Configuration](../README.md#configuration) section.

## The settings every device needs

| Device setting | Value | Notes |
|---|---|---|
| SMTP server / Host | The relay host's **LAN IP** (e.g. `192.168.1.10`) | Not `localhost` / `127.0.0.1`, even for apps on the same machine (see below) |
| Port | `2525` | Or whatever host port `docker-compose.yml` maps to the relay's 2525 |
| Encryption | **STARTTLS** (preferred) or **None** | Not "SSL/TLS": that is for port 465 and fails here |
| Verify peer / Verify certificate | **Off** | The relay's certificate is self-signed. Leave it on only if you gave the relay a trusted certificate (`TLS_CERT_FILE`) and use a matching host name |
| Authentication | On | |
| Username / Password | `SMTP_AUTH_USER` / `SMTP_AUTH_PASS` from `.env` | |
| From / Sender address | An address in `SMTP_ALLOWED_SENDERS` | Anything else is refused ("Sender not allowed") |
| Local Domain / EHLO name / Hostname | A short name for the device, e.g. `invoiceninja`, `scanner-2f` | **Optional but recommended**: this is the name the dashboard and log show for the device |

### Why set a Local Domain (EHLO name)?

When a device connects, it introduces itself with a name (the SMTP "EHLO"). The relay shows that name on the
dashboard and in every log line (`name=...`). Many apps leave it blank and introduce themselves as
`[127.0.0.1]`, which tells you nothing, and apps in Docker on the same host arrive from a Docker address. Setting
the Local Domain gives each device a recognisable name. It changes nothing about how mail is sent.

## Same machine as the relay: use the LAN IP

- **OpenMediaVault:** with `localhost` or `127.0.0.1`, OMV leaves Postfix's relay host empty and tries to deliver
  straight to the recipient's mail server on port 25 (usually blocked by the ISP), so nothing reaches the relay.
- **Apps in their own container:** `127.0.0.1` inside a container is the container itself, not the host.

Use the host's LAN IP for both. (Containers can also share the relay's Docker network and use `smtp-relay` as the
host; see the README.)

## Examples

### Invoice Ninja

Settings → Email Settings:

| Field | Value |
|---|---|
| Email Provider | SMTP |
| From Email | an address in `SMTP_ALLOWED_SENDERS` |
| Host | the relay host's LAN IP |
| Port | `2525` |
| Encryption | STARTTLS |
| Username / Password | the relay login |
| Local Domain (EHLO domain) | `invoiceninja` |
| Verify Peer | Off |

Then use **Send Test Email**.

### OpenMediaVault

System → Notification → Settings:

| Field | Value |
|---|---|
| SMTP server | the host's LAN IP (not `localhost`) |
| SMTP port | `2525` |
| Encryption mode | STARTTLS |
| Sender email | an address in `SMTP_ALLOWED_SENDERS` |
| Authentication | enabled, with the relay login |

Save, then **Apply** the pending configuration change (the yellow banner). Check that it took effect:
`postconf relayhost` should show the relay's address. Use **Send a test email**.

OMV does not offer an EHLO name setting; it appears on the dashboard under its DNS name (e.g. `nas.local`).

Turn off notification types you do not need (e.g. monitoring alerts) before switching: a noisy host can send
hundreds of messages a day, and Exchange Online limits a mailbox to 30 messages a minute.

## When it does not work

Open the dashboard. Problems show under **Needs attention** with the fix; the common ones:

| Dashboard shows | Meaning | Fix |
|---|---|---|
| TLS failed: certificate not trusted | The device verifies certificates | Turn off Verify peer, or install a trusted certificate |
| TLS failed: device expects SSL/TLS on connect | Encryption set to SSL/TLS | Set it to STARTTLS |
| Login failed / Slowed down | Wrong username or password (after 5 tries the relay checks one login every 10 seconds from that IP) | Fix the login on the device; the right password still gets in |
| Sender not allowed | From address not in `SMTP_ALLOWED_SENDERS` | Add it to `.env` (then `docker compose up -d`), or change the device's sender |
| Refused · From line mismatch / Sender line mismatch | The email's From (or Sender) line names a different address from the sender the device uses | Make them the same on the device |
| Refused · malformed headers | The email has broken header lines (for example a space before the colon in `From :`) | Update the device's firmware or software |
| Refused · too many recipients / headers too large | Over 500 recipients, or over 64 KB of header lines | Split the mailing |
| Turned away: too many connections | Over 50 open connections, or over 600 new ones a minute, from one IP | Look for a retry loop on that device or app |
| Disconnected: too much data before login | The device sent over 4 KB without logging in | Set it to log in with `SMTP_AUTH_USER` / `SMTP_AUTH_PASS` |
| Nothing appears at all | The device never reached the relay | Check host/IP and port; on OMV check `postconf relayhost` |

What the relay answers, for reading a device's own error log:

| Reply | Meaning |
|---|---|
| `421 4.7.0 Too many connections, try again later` | Per-IP connection limit; the device retries |
| `421 4.7.0 Too much data before login` / `... sent too many times` | The relay hung up on a misbehaving client |
| `451 4.3.1 Retry queue is full, try again later` | Microsoft 365 is unreachable and 2,000 messages (or 1 GB) are already waiting |
| `454 4.7.0 Too many failed logins, try again later` | Within 10 seconds of a checked login from an IP that has failed 5 times |
| `550 5.7.1 From header must match the sender address` | See "From line mismatch" above |
| `554 5.6.0 Malformed message headers` / `Message could not be processed` | The message itself is broken; retrying will not help |
