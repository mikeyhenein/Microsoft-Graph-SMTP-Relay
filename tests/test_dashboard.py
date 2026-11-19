"""The dashboard against a real relay and the fake Graph."""

import asyncio
import socket

import aiohttp
import pytest

from conftest import exchange
from version import __version__

DASH_USER = "admin"
DASH_PASS = "dashboard-password-for-tests"


@pytest.fixture
async def dash_relay(settings, start_relay):
    return await start_relay(settings, dashboard_user=DASH_USER, dashboard_pass=DASH_PASS,
                             dashboard_host="127.0.0.1", dashboard_port=0, dashboard_https=False)


@pytest.fixture
async def browser():
    # unsafe=True: keep cookies for an IP-address host like 127.0.0.1
    async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as session:
        yield session


def base(relay) -> str:
    return f"http://127.0.0.1:{relay.dashboard.port}"


async def sign_in(browser, relay, password=DASH_PASS) -> aiohttp.ClientResponse:
    async with browser.post(f"{base(relay)}/login", data={"username": DASH_USER, "password": password},
                            allow_redirects=False) as response:
        return response


async def test_signed_out_visitors_only_see_the_login_page(dash_relay, browser):
    async with browser.get(f"{base(dash_relay)}/", allow_redirects=False) as response:
        assert response.status == 303 and response.headers["Location"] == "/login"
    async with browser.get(f"{base(dash_relay)}/api/summary") as response:
        assert response.status == 401
    async with browser.post(f"{base(dash_relay)}/api/test-send", json={"from": "a@b.com", "to": "c@d.com"}) as r:
        assert r.status == 401
    async with browser.get(f"{base(dash_relay)}/login") as response:
        assert response.status == 200
        assert "Sign in" in await response.text()
        assert "default-src 'self'" in response.headers["Content-Security-Policy"]
        assert response.headers["X-Frame-Options"] == "DENY"


async def test_wrong_password_then_lockout(dash_relay, browser):
    response = await sign_in(browser, dash_relay, password="wrong")
    assert response.headers["Location"] == "/login?error=bad"
    async with browser.get(f"{base(dash_relay)}/login?error=bad") as page:
        assert "Wrong username or password." in await page.text()
    for _ in range(4):
        response = await sign_in(browser, dash_relay, password="wrong")
    assert response.headers["Location"] == "/login?error=locked"
    response = await sign_in(browser, dash_relay, password="wrong")  # one checked attempt per 10 s...
    response = await sign_in(browser, dash_relay)  # ...so the right password straight after is refused
    assert response.headers["Location"] == "/login?error=locked"
    dash_relay.dashboard.lockout.slow = 0.2
    await asyncio.sleep(0.3)
    response = await sign_in(browser, dash_relay)  # and gets in on the next checked attempt
    assert response.headers["Location"] == "/"


async def test_sign_in_shows_activity_and_sign_out_ends_the_session(dash_relay, browser):
    response = await sign_in(browser, dash_relay)
    assert response.headers["Location"] == "/"
    cookie = response.headers["Set-Cookie"]
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie and "Secure" not in cookie

    assert (await exchange(dash_relay.port)).data[0] == 250  # a "device" sends something
    async with browser.get(f"{base(dash_relay)}/api/summary") as response:
        assert response.status == 200
        summary = await response.json()
    assert summary["stats"]["sent"] == 1
    assert summary["devices"][0]["ip"] == "127.0.0.1" and summary["devices"][0]["name"] == "test-client"
    assert summary["messages"][0]["sender"] == "scanner@contoso.com"
    assert summary["messages"][0]["name"] == "test-client"  # the device's own name, kept per message
    assert summary["relay"]["allowed_senders"] == ["scanner@contoso.com", "*@lulu.test"]
    assert any(line["text"].startswith("message ") for line in summary["logs"])
    assert DASH_PASS not in str(summary)

    async with browser.get(f"{base(dash_relay)}/") as page:
        assert page.status == 200 and "Send a test" in await page.text()
    async with browser.post(f"{base(dash_relay)}/logout", allow_redirects=False) as response:
        assert response.status == 303
    async with browser.get(f"{base(dash_relay)}/api/summary") as response:
        assert response.status == 401


async def test_test_send_goes_through_the_relay(dash_relay, browser, fake_graph):
    await sign_in(browser, dash_relay)
    async with browser.post(f"{base(dash_relay)}/api/test-send",
                            json={"from": "scanner@contoso.com", "to": "me@example.com"}) as response:
        result = await response.json()
    assert result["ok"] is True and result["code"] == 250, result
    sent = fake_graph.sent_mime()
    assert b"From: scanner@contoso.com" in sent and b"To: me@example.com" in sent
    assert b"X-Relay-Test: dashboard" in sent
    assert f"Subject: smtp-graph-relay test message (v{__version__})".encode() in sent
    assert f"Relay version: {__version__}".encode() in sent and f"X-Relay-Version: {__version__}".encode() in sent

    async with browser.get(f"{base(dash_relay)}/api/summary") as response:
        summary = await response.json()
    [message] = summary["messages"]
    assert message["ip"] == "127.0.0.1" and message["result"] == "sent"
    assert message["tls"] == "yes"  # the test logs in with STARTTLS when the relay offers it


async def test_test_send_with_a_typed_from_that_is_not_allowed(dash_relay, browser, fake_graph):
    await sign_in(browser, dash_relay)
    async with browser.post(f"{base(dash_relay)}/api/test-send",
                            json={"from": "ap@fabrikam.com", "to": "me@example.com"}) as response:
        result = await response.json()
    assert result["ok"] is False and result["stage"] == "sender" and result["code"] == 550
    assert "SMTP_ALLOWED_SENDERS" in result["hint"] and "ap@fabrikam.com" in result["hint"]
    assert fake_graph.requests == []


async def test_test_send_with_a_typed_wildcard_domain_from(dash_relay, browser, fake_graph):
    await sign_in(browser, dash_relay)
    async with browser.post(f"{base(dash_relay)}/api/test-send",
                            json={"from": "printer@lulu.test", "to": "me@example.com"}) as response:
        result = await response.json()
    assert result["ok"] is True
    assert b"From: printer@lulu.test" in fake_graph.sent_mime()

    # A wildcard-allowed sender that has sent shows up as a From suggestion, after the .env addresses
    async with browser.get(f"{base(dash_relay)}/api/summary") as response:
        relay = (await response.json())["relay"]
    assert relay["sender_suggestions"] == [
        {"address": "scanner@contoso.com", "source": "allowed in .env"},
        {"address": "printer@lulu.test", "source": "used before"},
    ]
    assert relay["microsoft"]["ok"] is True and relay["smtp_port"] == 0


async def test_microsoft_sign_in_failure_shows_on_the_dashboard(settings, start_relay, browser):
    from conftest import StubMsal
    relay = await start_relay(settings, msal_app=StubMsal({"error": "invalid_client", "error_description": "expired"}),
                              dashboard_user=DASH_USER, dashboard_pass=DASH_PASS, dashboard_host="127.0.0.1",
                              dashboard_port=0, dashboard_https=False)
    await sign_in(browser, relay)
    async with browser.get(f"{base(relay)}/api/summary") as response:
        assert (await response.json())["relay"]["microsoft"]["ok"] is None  # not contacted yet
    assert (await exchange(relay.port)).data[1].startswith(b"2.0.0 Queued")
    async with browser.get(f"{base(relay)}/api/summary") as response:
        microsoft = (await response.json())["relay"]["microsoft"]
    assert microsoft["ok"] is False and "invalid_client" in microsoft["error"]


async def test_test_send_reports_queueing(dash_relay, browser, fake_graph):
    fake_graph.defaults["sendMail"] = 503
    await sign_in(browser, dash_relay)
    async with browser.post(f"{base(dash_relay)}/api/test-send",
                            json={"from": "scanner@contoso.com", "to": "me@example.com"}) as response:
        result = await response.json()
    assert result["ok"] is True and result["reply"].startswith("2.0.0 Queued as ")
    assert "queue" in result["hint"]
    async with browser.get(f"{base(dash_relay)}/api/summary") as response:
        summary = await response.json()
    assert summary["stats"]["queued"] == 1 and summary["queue"][0]["to"] == ["me@example.com"]


async def test_cancel_one_retry_then_all(dash_relay, browser, fake_graph, caplog):
    import logging
    caplog.set_level(logging.INFO, logger="relay")
    fake_graph.defaults["sendMail"] = 503
    for _ in range(3):
        assert (await exchange(dash_relay.port)).data[1].startswith(b"2.0.0 Queued")
    await sign_in(browser, dash_relay)
    async with browser.get(f"{base(dash_relay)}/api/summary") as response:
        queue = (await response.json())["queue"]
    assert len(queue) == 3

    async with browser.post(f"{base(dash_relay)}/api/queue/cancel", json={"id": queue[0]["id"]}) as response:
        assert (await response.json()) == {"cancelled": [queue[0]["id"]], "skipped": []}
    async with browser.get(f"{base(dash_relay)}/api/summary") as response:
        summary = await response.json()
    assert summary["stats"]["queued"] == 2
    assert {m["result"] for m in summary["messages"]} == {"queued", "cancelled"}
    line = next(r.getMessage() for r in caplog.records if r.getMessage().startswith("queue cancelled"))
    assert f"id={queue[0]['id']}" in line and "by=admin" in line and "ip=127.0.0.1" in line

    async with browser.post(f"{base(dash_relay)}/api/queue/cancel", json={"all": True}) as response:
        assert len((await response.json())["cancelled"]) == 2
    async with browser.get(f"{base(dash_relay)}/api/summary") as response:
        summary = await response.json()
    assert summary["queue"] == [] and {m["result"] for m in summary["messages"]} == {"cancelled"}


async def test_cancel_needs_sign_in_and_json(dash_relay, browser):
    async with browser.post(f"{base(dash_relay)}/api/queue/cancel", json={"all": True}) as response:
        assert response.status == 401
    await sign_in(browser, dash_relay)
    async with browser.post(f"{base(dash_relay)}/api/queue/cancel", data="all=true",
                            headers={"Content-Type": "application/x-www-form-urlencoded"}) as response:
        assert response.status == 415
    async with browser.post(f"{base(dash_relay)}/api/queue/cancel", json={"id": "../../etc/passwd"}) as response:
        result = await response.json()
    assert result == {"cancelled": [], "skipped": [{"id": "../../etc/passwd", "status": "not-found"}]}


async def test_test_send_validates_input_and_needs_json(dash_relay, browser, fake_graph):
    await sign_in(browser, dash_relay)
    async with browser.post(f"{base(dash_relay)}/api/test-send", json={"from": "not an address", "to": "x@y.com"}) as r:
        assert (await r.json())["stage"] == "input"
    async with browser.post(f"{base(dash_relay)}/api/test-send", data="from=a@b.com&to=c@d.com",
                            headers={"Content-Type": "application/x-www-form-urlencoded"}) as r:
        assert r.status == 415  # a cross-site form post cannot send JSON without a CORS preflight
    assert fake_graph.requests == []


async def test_https_dashboard_marks_the_cookie_secure(settings, start_relay):
    relay = await start_relay(settings, dashboard_user=DASH_USER, dashboard_pass=DASH_PASS,
                              dashboard_host="127.0.0.1", dashboard_port=0, dashboard_https=True)
    assert relay.dashboard.url.startswith("https://")
    async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as browser:
        async with browser.post(f"https://127.0.0.1:{relay.dashboard.port}/login", ssl=False, allow_redirects=False,
                                data={"username": DASH_USER, "password": DASH_PASS}) as response:
            assert response.headers["Location"] == "/"
            assert "Secure" in response.headers["Set-Cookie"]
            assert "Strict-Transport-Security" in response.headers


async def test_less_to_fingerprint_before_sign_in(dash_relay, browser):
    async with browser.get(f"{base(dash_relay)}/login") as response:
        assert __version__ not in await response.text()
        assert response.headers["Server"] == "relay"
    for method, path in (("GET", "/nope"), ("PUT", "/api/summary")):
        async with browser.request(method, f"{base(dash_relay)}{path}") as response:
            assert response.status in (404, 405)
            assert "default-src 'self'" in response.headers["Content-Security-Policy"]
            assert response.headers["Server"] == "relay"
    greeting = await asyncio.to_thread(lambda: socket.create_connection(("127.0.0.1", dash_relay.port), 5).recv(200))
    assert b"relay" not in greeting.lower() and greeting.startswith(b"220 ")


async def test_cross_site_posts_are_refused(dash_relay, browser):
    for headers in ({"Sec-Fetch-Site": "cross-site"}, {"Origin": "https://evil.example"}, {"Origin": "null"}):
        async with browser.post(f"{base(dash_relay)}/login", data={"username": DASH_USER, "password": "wrong"},
                                headers=headers, allow_redirects=False) as response:
            assert response.status == 403
    assert not dash_relay.dashboard.lockout.throttled("127.0.0.1")  # those never counted as failures
    same = {"Sec-Fetch-Site": "same-origin", "Origin": base(dash_relay)}
    async with browser.post(f"{base(dash_relay)}/login", data={"username": DASH_USER, "password": DASH_PASS},
                            headers=same, allow_redirects=False) as response:
        assert response.headers["Location"] == "/"


async def test_test_send_is_rate_limited(dash_relay, browser, fake_graph):
    await sign_in(browser, dash_relay)
    replies = []
    for i in range(6):
        async with browser.post(f"{base(dash_relay)}/api/test-send",
                                json={"from": "scanner@contoso.com", "to": f"me{i}@example.com"}) as response:
            replies.append(await response.json())
    assert [r["ok"] for r in replies] == [True] * 5 + [False]
    assert replies[-1]["reply"] == "Too many test messages, wait a minute."
    assert len(fake_graph.of("/sendMail")) == 5


async def test_sign_out_ends_every_session(dash_relay, browser):
    await sign_in(browser, dash_relay)
    async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as other:
        await sign_in(other, dash_relay)
        async with browser.post(f"{base(dash_relay)}/logout", allow_redirects=False):
            pass
        async with other.get(f"{base(dash_relay)}/api/summary") as response:
            assert response.status == 401


async def test_dashboard_is_off_without_credentials(relay):
    assert relay.dashboard is None


def test_static_files_never_build_html_from_data():
    from pathlib import Path
    script = (Path(__file__).resolve().parents[1] / "app" / "dashboard_static" / "app.js").read_text()
    for risky in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
        assert risky not in script
