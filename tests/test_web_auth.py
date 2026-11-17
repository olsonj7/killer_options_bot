"""Login form + signed session cookie for the dashboard (real HTTP round trips)."""

from __future__ import annotations

import http.client
import threading
import time
from http.server import ThreadingHTTPServer
from urllib.parse import urlencode

import pytest
from test_web import _write_config

from killer_options_bot.web import (
    SESSION_COOKIE,
    Dashboard,
    _LoginLimiter,
    _make_handler,
    _safe_next,
    _session_key,
    _sign_session,
    _verify_session,
)

USER, PWD = "jeff", "correct horse battery"


@pytest.fixture
def server(tmp_path):
    started = []

    def start(auth=(USER, PWD)):
        dash = Dashboard(_write_config(tmp_path), source="mock")
        srv = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(dash, auth=auth))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        started.append(srv)
        return srv.server_address[1]

    yield start
    for srv in started:
        srv.shutdown()
        srv.server_close()


def _req(port, method, path, body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    hdrs = dict(headers or {})
    data = None
    if body is not None:
        data = urlencode(body)
        hdrs["Content-Type"] = "application/x-www-form-urlencoded"
    conn.request(method, path, body=data, headers=hdrs)
    resp = conn.getresponse()
    text = resp.read().decode("utf-8", "replace")
    out = (resp.status, dict(resp.getheaders()), text)
    conn.close()
    return out


def _login(port, password=PWD, **extra):
    return _req(port, "POST", "/login",
                {"username": USER, "password": password, **extra})


def _cookie(headers) -> str:
    return headers["Set-Cookie"].split(";", 1)[0]  # "kob_session=<token>"


# --- helpers ---------------------------------------------------------------


def test_session_token_round_trip_and_rejections():
    key = _session_key(USER, PWD)
    now = time.time()
    token = _sign_session(key, USER, int(now) + 100)
    assert _verify_session(key, token, USER, now) == int(now) + 100
    assert _verify_session(key, token, USER, now + 200) is None          # expired
    assert _verify_session(key, token, "someone", now) is None           # wrong user
    assert _verify_session(_session_key(USER, "other"), token, USER, now) is None
    payload, sig = token.split(".")
    assert _verify_session(key, payload + "." + "0" * len(sig), USER, now) is None
    assert _verify_session(key, "garbage", USER, now) is None
    assert _verify_session(key, "", USER, now) is None
    assert _verify_session(key, "é.é", USER, now) is None                # non-ASCII


@pytest.mark.parametrize("target,expected", [
    ("/config", "/config"), ("/?range=30", "/?range=30"),
    ("//evil.com", "/"), ("https://evil.com", "/"), ("evil", "/"),
    ("/login", "/"), ("/logout", "/"), ("/a\r\nSet-Cookie: x=1", "/"),
    ("/\\evil.com", "/"), (None, "/"), ("", "/"),
])
def test_safe_next(target, expected):
    assert _safe_next(target) == expected


def test_limiter_blocks_then_expires():
    now = [0.0]
    lim = _LoginLimiter(max_fails=3, window=60, clock=lambda: now[0])
    for _ in range(3):
        assert not lim.blocked("a")
        lim.record_failure("a")
    assert lim.blocked("a") and not lim.blocked("b")
    now[0] = 61
    assert not lim.blocked("a")
    lim.record_failure("a")
    lim.reset("a")
    assert not lim.blocked("a")


# --- end-to-end ------------------------------------------------------------


def test_unauthenticated_pages_redirect_to_login(server):
    port = server()
    status, headers, _ = _req(port, "GET", "/config")
    assert status == 303 and headers["Location"] == "/login?next=%2Fconfig"
    assert _req(port, "GET", "/export.csv")[0] == 303
    assert "WWW-Authenticate" not in headers  # no browser Basic pop-up


def test_login_page_renders_phone_friendly_form(server):
    status, _, page = _req(server(), "GET", "/login")
    assert status == 200
    assert 'autocomplete="current-password"' in page and 'name="password"' in page


def test_login_success_sets_cookie_and_unlocks_dashboard(server):
    port = server()
    status, headers, _ = _login(port, next="/config")
    assert status == 303 and headers["Location"] == "/config"
    cookie_line = headers["Set-Cookie"]
    assert "HttpOnly" in cookie_line and "SameSite=Lax" in cookie_line
    assert f"Max-Age={30 * 86400}" in cookie_line and "Secure" not in cookie_line

    status, _, page = _req(port, "GET", "/", headers={"Cookie": _cookie(headers)})
    assert status == 200 and "action='/logout'" in page


def test_cookie_marked_secure_behind_https_proxy(server):
    _, headers, _ = _req(server(), "POST", "/login",
                         {"username": USER, "password": PWD},
                         headers={"X-Forwarded-Proto": "https"})
    assert "Secure" in headers["Set-Cookie"]


def test_wrong_password_is_rejected_without_cookie(server):
    status, headers, page = _login(server(), password="nope")
    assert status == 401 and "Set-Cookie" not in headers
    assert "Invalid username or password" in page


def test_actions_need_a_session(server):
    port = server()
    status, headers, _ = _req(port, "POST", "/manage")
    assert status == 303 and headers["Location"] == "/login"


def test_tampered_and_foreign_cookies_rejected(server):
    port = server()
    cookie = _cookie(_login(port)[1])
    assert _req(port, "GET", "/", headers={"Cookie": cookie[:-3] + "abc"})[0] == 303
    foreign = _sign_session(_session_key(USER, "other"), USER, int(time.time()) + 999)
    assert _req(port, "GET", "/",
                headers={"Cookie": f"{SESSION_COOKIE}={foreign}"})[0] == 303
    expired = _sign_session(_session_key(USER, PWD), USER, int(time.time()) - 5)
    assert _req(port, "GET", "/",
                headers={"Cookie": f"{SESSION_COOKIE}={expired}"})[0] == 303


def test_open_redirect_blocked(server):
    _, headers, _ = _login(server(), next="//evil.com/x")
    assert headers["Location"] == "/"


def test_logout_clears_cookie(server):
    port = server()
    cookie = _cookie(_login(port)[1])
    status, headers, _ = _req(port, "POST", "/logout", headers={"Cookie": cookie})
    assert status == 303 and headers["Location"] == "/login"
    assert "Max-Age=0" in headers["Set-Cookie"]


def test_session_near_expiry_is_renewed(server):
    port = server()
    soon = _sign_session(_session_key(USER, PWD), USER, int(time.time()) + 86400)
    status, headers, _ = _req(
        port, "GET", "/", headers={"Cookie": f"{SESSION_COOKIE}={soon}"}
    )
    assert status == 200 and f"Max-Age={30 * 86400}" in headers["Set-Cookie"]
    fresh = _cookie(_login(port)[1])  # far from expiry -> no new cookie
    assert "Set-Cookie" not in _req(port, "GET", "/", headers={"Cookie": fresh})[1]


def test_bruteforce_is_locked_out_even_for_the_right_password(server):
    port = server()
    for _ in range(5):
        assert _login(port, password="bad")[0] == 401
    status, _, page = _login(port)  # correct password, but locked out
    assert status == 429 and "Too many attempts" in page


def test_basic_auth_still_works_for_scripts(server):
    import base64

    port = server()
    token = base64.b64encode(f"{USER}:{PWD}".encode()).decode()
    assert _req(port, "GET", "/export.csv",
                headers={"Authorization": f"Basic {token}"})[0] == 200
    bad = base64.b64encode(f"{USER}:wrong".encode()).decode()
    assert _req(port, "GET", "/export.csv",
                headers={"Authorization": f"Basic {bad}"})[0] == 303


def test_no_auth_means_no_login(server):
    port = server(auth=None)
    status, _, page = _req(port, "GET", "/")
    assert status == 200 and "action='/logout'" not in page
    assert _req(port, "GET", "/login")[0] == 303
