import base64
import json
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser

import pytest

from conftest import StubMsal
from handlers.graph_client import UPLOAD_SESSION_MIN, GraphClient, extract_body_and_attachments, header_addresses
from retry import DeliveryError

JPEG = b"\xff\xd8\xff\xe0" + b"j" * 100


def parse(raw: bytes) -> EmailMessage:
    return BytesParser(policy=policy.default).parsebytes(raw)


def rich_message(pdf: bytes = b"%PDF-1.7 tiny") -> bytes:
    msg = EmailMessage()
    msg["From"] = "scanner@contoso.com"
    msg["To"] = '"Doe, John" <john@example.com>, jane@example.com'
    msg["Subject"] = "=?utf-8?q?Scan_r=C3=A9sum=C3=A9?="
    msg.set_content("plain version")
    msg.add_alternative('<p>html version <img src="cid:logo"></p>', subtype="html")
    msg.get_payload()[1].add_related(JPEG, maintype="image", subtype="jpeg", cid="<logo>")
    msg.add_attachment(pdf, maintype="application", subtype="pdf", filename="résumé.pdf")
    return msg.as_bytes()


def test_header_addresses_keep_commas_inside_names():
    assert header_addresses(parse(rich_message()), "To") == [("Doe, John", "john@example.com"),
                                                             ("", "jane@example.com")]


def test_extracts_html_body_inline_image_and_attachment():
    body, content_type, attachments = extract_body_and_attachments(parse(rich_message()))
    assert content_type == "html" and "html version" in body
    by_name = {a.name: a for a in attachments}
    assert by_name["résumé.pdf"].data == b"%PDF-1.7 tiny" and not by_name["résumé.pdf"].is_inline
    [image] = [a for a in attachments if a.content_type == "image/jpeg"]
    assert image.is_inline and image.content_id == "logo" and image.data == JPEG


def test_attachment_only_message_does_not_crash():
    msg = EmailMessage()
    msg["From"] = "scanner@contoso.com"
    msg.set_content(b"%PDF", maintype="application", subtype="pdf", disposition="attachment", filename="a.pdf")
    body, content_type, attachments = extract_body_and_attachments(parse(msg.as_bytes()))
    assert (body, content_type) == ("", "text")
    assert [a.name for a in attachments] == ["a.pdf"]


async def client(fake_graph, **kwargs) -> GraphClient:
    graph = GraphClient(StubMsal(), base_url=fake_graph.url, **kwargs)
    await graph.__aenter__()
    return graph


async def test_send_mime_posts_the_raw_message(fake_graph):
    graph = await client(fake_graph)
    raw = rich_message()
    try:
        assert await graph.send("Scanner@contoso.com", raw) == 202
    finally:
        await graph.__aexit__(None, None, None)
    [request] = fake_graph.of("/sendMail")
    assert request.path == "/v1.0/users/Scanner@contoso.com/sendMail"
    assert request.headers["Authorization"] == "Bearer test-token"
    assert request.headers["Content-Type"] == "text/plain"
    assert base64.b64decode(request.body) == raw


async def test_too_big_without_large_messages_is_permanent(fake_graph):
    graph = await client(fake_graph, small_limit=100)
    try:
        with pytest.raises(DeliveryError) as caught:
            await graph.send("scanner@contoso.com", b"x" * 101)
    finally:
        await graph.__aexit__(None, None, None)
    assert not caught.value.transient and caught.value.status == 413
    assert fake_graph.requests == []


async def test_graph_errors_carry_status_code_and_retry_after(fake_graph):
    fake_graph.script("sendMail", (429, 7))
    graph = await client(fake_graph)
    try:
        with pytest.raises(DeliveryError) as caught:
            await graph.send("scanner@contoso.com", b"Subject: x\r\n\r\nhi")
    finally:
        await graph.__aexit__(None, None, None)
    error = caught.value
    assert error.transient and error.status == 429 and error.retry_after == 7 and error.code == "Fake429"


async def test_token_failure_is_transient_and_never_calls_graph(fake_graph):
    graph = GraphClient(StubMsal({"error": "invalid_client", "error_description": "AADSTS7000215: bad secret\nTrace"}),
                        base_url=fake_graph.url)
    async with graph:
        with pytest.raises(DeliveryError) as caught:
            await graph.send("scanner@contoso.com", b"Subject: x\r\n\r\nhi")
    assert caught.value.transient and "invalid_client" in str(caught.value) and "Trace" not in str(caught.value)
    assert fake_graph.requests == []


async def test_large_message_uses_draft_attachments_and_upload_session(fake_graph):
    big_pdf = bytes(range(256)) * ((UPLOAD_SESSION_MIN + 1_500_000) // 256)
    raw = rich_message(pdf=big_pdf)
    graph = await client(fake_graph, large_messages=True, small_limit=1000)
    try:
        assert await graph.send("scanner@contoso.com", raw) == 202
    finally:
        await graph.__aexit__(None, None, None)

    [create] = fake_graph.of("/scanner@contoso.com/messages")
    assert create.headers["Prefer"] == 'IdType="ImmutableId"'
    draft = json.loads(create.body)
    assert draft["subject"] == "Scan résumé"
    assert draft["body"]["contentType"] == "html"
    assert [r["emailAddress"]["address"] for r in draft["toRecipients"]] == ["john@example.com", "jane@example.com"]

    # The small inline image is posted directly; the big PDF goes through an upload session.
    [small] = fake_graph.of("/attachments")
    posted = json.loads(small.body)
    assert posted["isInline"] is True and posted["contentId"] == "logo"
    [session] = fake_graph.of("/createUploadSession")
    item = json.loads(session.body)["AttachmentItem"]
    assert item["name"] == "résumé.pdf" and item["size"] == len(big_pdf) and item["isInline"] is False

    chunks = fake_graph.of("", method="PUT")
    assert len(chunks) == 2
    assert all("Authorization" not in c.headers for c in chunks)
    assert chunks[0].headers["Content-Range"] == f"bytes 0-{3 * 1024 * 1024 - 1}/{len(big_pdf)}"
    assert chunks[1].headers["Content-Range"].endswith(f"-{len(big_pdf) - 1}/{len(big_pdf)}")
    assert b"".join(c.body for c in chunks) == big_pdf
    assert len(fake_graph.of("/send")) == 1


async def test_failed_large_send_deletes_the_draft(fake_graph):
    fake_graph.script("send", 503)
    graph = await client(fake_graph, large_messages=True, small_limit=10)
    try:
        with pytest.raises(DeliveryError) as caught:
            await graph.send("scanner@contoso.com", rich_message())
    finally:
        await graph.__aexit__(None, None, None)
    assert caught.value.transient
    [delete] = fake_graph.of("", method="DELETE")
    assert delete.path.endswith("/messages/AAMkDraft==")
