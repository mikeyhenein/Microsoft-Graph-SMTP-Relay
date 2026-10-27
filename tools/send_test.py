"""
Send a real test message through a running relay: HTML body, an inline image and a PDF.

    python tools/send_test.py --to you@contoso.com [--starttls] [--bcc other@contoso.com] [--big]

Settings come from .env (SMTP_AUTH_USER, SMTP_AUTH_PASS, TEST_FROM_ADDRESS) unless given as options.
This sends real mail.
"""

import argparse
import os
import smtplib
import ssl
from email.message import EmailMessage
from pathlib import Path

from dotenv import load_dotenv

HERE = Path(__file__).resolve().parent


def build_message(sender: str, to: str, big: bool) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = "Relay test with HTML, inline image and =?utf-8?q?attachm=C3=A9nt?="
    msg["From"] = sender
    msg["To"] = to
    msg.set_content("This is a test message from smtp-graph-relay (plain-text version).")
    msg.add_alternative(
        '<p>Hi!<br>This test message has an HTML body, an inline image and a PDF attachment.</p>'
        '<img src="cid:image1" alt="test image">',
        subtype="html",
    )
    msg.get_payload()[1].add_related((HERE / "image.jpg").read_bytes(), maintype="image", subtype="jpeg",
                                     cid="<image1>", filename="image.jpg")
    msg.add_attachment((HERE / "doc.pdf").read_bytes(), maintype="application", subtype="pdf", filename="doc.pdf")
    if big:
        # ~5 MB of incompressible data: exercises the LARGE_MESSAGES draft + upload path.
        msg.add_attachment(os.urandom(5 * 1024 * 1024), maintype="application", subtype="octet-stream",
                           filename="large-test.bin")
    return msg


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--to", default=os.environ.get("TEST_TO_ADDRESS"), required="TEST_TO_ADDRESS" not in os.environ)
    parser.add_argument("--from", dest="sender", default=os.environ.get("TEST_FROM_ADDRESS"))
    parser.add_argument("--bcc", action="append", default=[], help="envelope-only recipient (repeatable)")
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=int(os.environ.get("SMTP_RELAY_PORT", 2525)))
    parser.add_argument("--user", default=os.environ.get("SMTP_AUTH_USER"))
    parser.add_argument("--password", default=os.environ.get("SMTP_AUTH_PASS"))
    parser.add_argument("--starttls", action="store_true", help="upgrade to TLS before logging in")
    parser.add_argument("--big", action="store_true", help="add a 5 MB attachment")
    args = parser.parse_args()
    if not args.sender:
        parser.error("--from (or TEST_FROM_ADDRESS) is required")

    msg = build_message(args.sender, args.to, args.big)
    with smtplib.SMTP(args.host, args.port, timeout=120) as client:
        client.ehlo()
        if args.starttls:
            context = ssl.create_default_context()
            # The relay's default certificate is self-signed.
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            client.starttls(context=context)
            client.ehlo()
        if args.user:
            client.login(args.user, args.password or "")
        refused = client.send_message(msg, to_addrs=[args.to, *args.bcc])
    print("Sent." if not refused else f"Some recipients were refused: {refused}")


if __name__ == "__main__":
    main()
