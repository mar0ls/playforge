"""Cross-site request forgery: state-changing requests must prove same-origin.

Until now the only thing standing between a hostile page and this API was
`SameSite=Lax` on the session cookie. That has two holes. A browser that ignores
SameSite, or a same-site subdomain an attacker controls, is not covered — the
caveat SECURITY.md already stated. And in the mode README calls the default
(no accounts, no `ANSIBLE_GUI_PASSWORD`) there is no session cookie at all, so
there is nothing for SameSite to withhold: a page open in the same browser can
drive an instance listening on 127.0.0.1.

The JSON endpoints are shielded from that by the browser's CORS preflight rather
than by anything this app does. `import-zip` takes `multipart/form-data`, which a
plain HTML form can send with no preflight at all — so it is the shape used here.

Everything runs through TestClient, which sends exactly what a browser would put
on the wire, minus the browser's own decisions. That is the point: these tests
are about what the server accepts, not about what a browser bothers to send.
"""
from __future__ import annotations

import io
import zipfile

import pytest

pytest.importorskip("aiosqlite")
pytest.importorskip("cryptography")

from starlette.testclient import TestClient

EVIL = "https://evil.example"


def _zip_bytes() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("site.yml", "---\n- hosts: all\n  tasks: []\n")
    return buf.getvalue()


@pytest.fixture
def open_client(clean_users, monkeypatch):
    """The default deployment: no accounts, no shared password, no cookies."""
    monkeypatch.delenv("ANSIBLE_GUI_PASSWORD", raising=False)
    from app.main import app
    with TestClient(app) as c:
        yield c


# --- the multipart vector, in the default mode -------------------------------

def test_cross_site_multipart_upload_is_refused(open_client):
    """A hostile page can POST multipart to localhost with no preflight.

    Nothing about this request is unusual for a browser to send: an HTML form
    with enctype=multipart/form-data, aimed at 127.0.0.1, submitted by script on
    a page the user happens to have open.
    """
    response = open_client.post(
        "/api/projects/import-zip",
        headers={"Origin": EVIL},
        data={"name": "forged", "description": ""},
        files={"upload": ("p.zip", _zip_bytes(), "application/zip")},
    )
    assert response.status_code == 403, (
        f"a cross-site upload was accepted with {response.status_code}"
    )


def test_cross_site_json_post_is_refused(open_client):
    """A browser's CORS preflight would stop this one before it arrived — but
    that is the browser's doing, not the app's, and it is not a defence the app
    can point to."""
    response = open_client.post(
        "/api/projects", headers={"Origin": EVIL}, json={"name": "forged"}
    )
    assert response.status_code == 403


def test_cross_site_delete_is_refused(open_client):
    response = open_client.delete(
        "/api/projects/whatever", headers={"Origin": EVIL}
    )
    assert response.status_code == 403


# --- same-origin traffic must keep working -----------------------------------

def test_same_origin_post_still_works(open_client):
    """The whole app is same-origin fetch; breaking it would break everything."""
    response = open_client.post(
        "/api/projects",
        headers={"Origin": "http://testserver"},
        json={"name": "legitimate"},
    )
    assert response.status_code == 200, response.text

    # Clean up the project this created.
    project_id = response.json()["id"]
    open_client.delete(f"/api/projects/{project_id}",
                       headers={"Origin": "http://testserver"})


def test_a_request_with_no_origin_still_works(open_client):
    """curl, scripts and the lab-regression Makefile target send no Origin at
    all. Refusing those would break every non-browser client for no gain: an
    attacker's page cannot suppress the header."""
    response = open_client.post("/api/projects", json={"scripted": "yes", "name": "scripted"})
    assert response.status_code == 200, response.text

    project_id = response.json()["id"]
    open_client.delete(f"/api/projects/{project_id}")


def test_reads_are_never_blocked(open_client):
    """Only state-changing methods are checked; a cross-site GET can't read the
    response anyway, and blocking it would break nothing useful."""
    assert open_client.get("/api/projects", headers={"Origin": EVIL}).status_code == 200


# --- the browser's own verdict, when it sends one ----------------------------

def _post(client, **headers):
    return client.post("/api/projects", headers=headers, json={"name": "sec-fetch"})


def test_sec_fetch_site_cross_site_is_refused(open_client):
    assert _post(open_client, **{"Sec-Fetch-Site": "cross-site"}).status_code == 403


def test_sec_fetch_site_same_site_is_refused(open_client):
    """A subdomain an attacker controls is same-site but not same-origin — the
    exact case `SameSite=Lax` never covered."""
    assert _post(open_client, **{"Sec-Fetch-Site": "same-site"}).status_code == 403


def test_sec_fetch_site_same_origin_is_allowed(open_client):
    response = _post(open_client, **{"Sec-Fetch-Site": "same-origin"})
    assert response.status_code == 200, response.text
    open_client.delete(f"/api/projects/{response.json()['id']}")


def test_sec_fetch_site_none_is_allowed(open_client):
    """`none` is a typed URL or a bookmark — the user's own doing."""
    response = _post(open_client, **{"Sec-Fetch-Site": "none"})
    assert response.status_code == 200, response.text
    open_client.delete(f"/api/projects/{response.json()['id']}")


def test_the_browser_verdict_wins_over_a_matching_origin(open_client):
    """A forged Origin must not talk the guard out of the browser's own label."""
    response = open_client.post(
        "/api/projects",
        headers={"Sec-Fetch-Site": "cross-site", "Origin": "http://testserver"},
        json={"name": "forged"},
    )
    assert response.status_code == 403


def test_the_refusal_still_carries_the_security_headers(open_client):
    """The guard sits inside security_headers, so a 403 is not a bare response."""
    response = _post(open_client, **{"Sec-Fetch-Site": "cross-site"})
    assert response.status_code == 403
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert "Content-Security-Policy" in response.headers


# --- the token layer ---------------------------------------------------------
#
# The Origin check above covers a browser that honours SameSite. These cover the
# one that does not: it sends our cookie cross-site, and the attacker still has
# to produce a token signed for that exact nonce.

import re

from app.core import csrf


def _token_from(html: str) -> str:
    match = re.search(r'name="csrf-token" content="([^"]+)"', html)
    assert match, "no csrf-token meta tag in the page"
    return match.group(1)


def test_a_page_carries_a_token_and_sets_the_cookie(open_client):
    response = open_client.get("/")
    assert response.status_code == 200
    assert _token_from(response.text)

    cookie = response.headers.get("set-cookie", "")
    assert csrf.COOKIE in cookie
    # The page hands the value over in the meta tag; script never reads the cookie.
    assert "HttpOnly" in cookie


def test_once_the_cookie_exists_a_write_without_a_token_is_refused(open_client):
    open_client.get("/")  # picks up the cookie, as a browser would
    response = open_client.post("/api/projects", json={"name": "no token"})
    assert response.status_code == 403
    assert "CSRF" in response.json()["detail"]


def test_a_write_with_the_matching_token_is_allowed(open_client):
    token = _token_from(open_client.get("/").text)
    response = open_client.post("/api/projects", json={"name": "with token"},
                                headers={"X-CSRF-Token": token})
    assert response.status_code == 200, response.text
    open_client.delete(f"/api/projects/{response.json()['id']}",
                       headers={"X-CSRF-Token": token})


def test_a_token_signed_for_another_nonce_is_refused(open_client):
    """Planting a cookie buys nothing: the token has to be signed for it."""
    open_client.get("/")
    forged = csrf.token_for(csrf.new_nonce())
    response = open_client.post("/api/projects", json={"name": "forged"},
                                headers={"X-CSRF-Token": forged})
    assert response.status_code == 403


def test_an_unsigned_nonce_echo_is_refused(open_client):
    """Plain double-submit would accept this. Signed double-submit does not."""
    open_client.get("/")
    nonce = open_client.cookies.get(csrf.COOKIE)
    response = open_client.post("/api/projects", json={"name": "echo"},
                                headers={"X-CSRF-Token": nonce})
    assert response.status_code == 403


def test_a_client_with_no_cookie_is_left_to_the_origin_check(open_client):
    """Scripts and curl never pick up the cookie, and a forged cross-site POST
    arrives without it too — that one is refused by Origin, not by this."""
    open_client.cookies.clear()
    response = open_client.post("/api/projects", json={"name": "scripted"})
    assert response.status_code == 200, response.text
    open_client.delete(f"/api/projects/{response.json()['id']}")


def test_reads_need_no_token(open_client):
    open_client.get("/")
    assert open_client.get("/api/projects").status_code == 200


# --- the three form posts ----------------------------------------------------

def test_login_without_a_token_is_refused(clean_users, monkeypatch):
    monkeypatch.setenv("ANSIBLE_GUI_PASSWORD", "letmein")
    from app.main import app
    with TestClient(app) as c:
        c.get("/login")
        response = c.post("/login", data={"password": "letmein"}, follow_redirects=False)
        assert response.status_code == 403


def test_login_with_the_form_token_succeeds(clean_users, monkeypatch):
    monkeypatch.setenv("ANSIBLE_GUI_PASSWORD", "letmein")
    from app.main import app
    with TestClient(app) as c:
        page = c.get("/login")
        token = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
        response = c.post("/login", data={"password": "letmein", "csrf_token": token},
                          follow_redirects=False)
        assert response.status_code == 303, response.text


def test_logout_without_a_token_is_refused(open_client):
    open_client.get("/")
    assert open_client.post("/logout", follow_redirects=False).status_code == 403


# --- the WebSocket handshake -------------------------------------------------
#
# HTTP middleware never sees the WS scope, and `new WebSocket()` cannot set a
# header, so Origin is the only thing a handshake can be judged on.

def test_a_cross_site_websocket_handshake_is_closed(open_client):
    from starlette.websockets import WebSocketDisconnect as WSDisconnect

    with pytest.raises((WSDisconnect, Exception)) as exc:
        with open_client.websocket_connect("/api/runs/ws", headers={"Origin": EVIL}) as ws:
            ws.send_json({"project_id": "x", "playbook": "p.yml"})
            ws.receive_json()
    assert exc.value is not None


def test_a_same_origin_websocket_handshake_is_accepted(open_client):
    """It must still open — the run screen is the app's main feature."""
    with open_client.websocket_connect(
        "/api/runs/ws", headers={"Origin": "http://testserver"}
    ) as ws:
        ws.send_json({"project_id": "no-such-project", "playbook": "p.yml"})
        # Reaching the handler at all is the point; the run itself fails on the
        # missing project, which is a different layer's business.
        assert ws.receive_json()["event"] == "error"
