# Middleware support

The relay has a small `EventBus` that Python middleware can subscribe to. Every `*.py` file in
`MIDDLEWARE_DIR` (default: the `middleware` folder next to the code, `/usr/src/app/middleware` in the container)
is imported at startup. If it defines a `Middleware` class, that class is created with the relay's handler. The
startup log lists which modules loaded or failed (`middleware loaded ... modules=...`).

In Docker, mount your files into the container, for example
`- ./middleware:/usr/src/app/middleware:ro` under `volumes:` in `docker-compose.yml`.

## Rules

- Handlers may be plain functions or `async` functions.
- **Fail closed:** if a handler for `before_send`, `sender`, `recipients` or `skip_send` raises, the message is
  not sent. The device gets `451` (try again later), so a broken compliance hook never lets mail out unchecked.
  A failing `after_send` handler is only logged, since the mail has already gone.
- Hooks run **once** per message, before the first send attempt. If the message ends up in the retry queue, it is
  stored as the hooks left it, and queued retries do not run the hooks again.
- Changes to the `sender` / `recipients` lists are written back into the message's To, Cc, Bcc and Reply-To
  headers.
- `msGraphHandler.app` is the MSAL `ConfidentialClientApplication`, kept for middleware written for v1.

## Examples

Skip sending (for example on a test system):

```python
# middleware/skip_sendmail.py
from event_bus import event_bus_instance
from handlers.microsoft_graph import MicrosoftGraphHandler

class Middleware:
    def __init__(self, msGraphHandler: MicrosoftGraphHandler):
        event_bus_instance.subscribe('skip_send', self.skip_sendmail)

    def skip_sendmail(self):
        # return True to skip email submission
        return True
```

Add a prefix to the subject:

```python
import logging
from email.message import EmailMessage

from event_bus import event_bus_instance
from handlers.microsoft_graph import MicrosoftGraphHandler

class Middleware:
    """Adds a prefix to the Subject header."""

    def __init__(self, msGraphHandler: MicrosoftGraphHandler):
        event_bus_instance.subscribe('before_send', self.modify_mail)

    def modify_mail(self, mail_message: EmailMessage):
        logging.info("ReplaceSubject -> Adding prefix to subject")
        mail_message.replace_header('Subject', '[ReplaceSubject middleware] ' + mail_message['Subject'])
```

Overwrite the Reply-To header:

```python
from event_bus import event_bus_instance
from handlers.microsoft_graph import MicrosoftGraphHandler

class Middleware:
    """Overwrites the Reply-To header."""

    def __init__(self, msGraphHandler: MicrosoftGraphHandler):
        event_bus_instance.subscribe('sender', self.overwrite_reply_to)

    async def overwrite_reply_to(self, sender_mail: str | None, reply_to: list):
        reply_to.clear()
        reply_to.append({"emailAddress": {"address": "some@email.tld"}})
```

## EventBus

`event_bus_instance` has `subscribe(event, handler)`, `unsubscribe(event, handler)`, `publish(event, *args)`
(async) and `publishSync(event, *args)`.

```python
# subscribe to the "sender" event and call my_sender_func when it is published
from event_bus import event_bus_instance
event_bus_instance.subscribe('sender', self.my_sender_func)
```

## Events

| Event name  | Arguments                | When / what it does                                                                 |
| ----------- | ------------------------ | ----------------------------------------------------------------------------------- |
| before_auth | auth_data                | Before a login is checked (`auth_data.login`, `auth_data.password` are bytes)       |
| after_auth  | auth_data                | After a successful login                                                            |
| before_send | email_message            | The parsed `EmailMessage`; changes to it are sent                                   |
| sender      | mail_from, reply_to      | Envelope sender and the Reply-To list (Graph `emailAddress` dicts, editable)        |
| recipients  | to, cc, bcc              | Recipient lists (Graph `emailAddress` dicts, editable)                              |
| skip_send   |                          | Return `True` to accept the message without sending it                              |
| after_send  |                          | After Microsoft 365 accepted the message (including later sends from the queue)     |

`before_auth` / `after_auth` run inside the login step, which cannot wait: plain handlers run immediately, and
`async` handlers are started but not waited for.
