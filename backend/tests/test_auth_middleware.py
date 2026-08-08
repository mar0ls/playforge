"""End-to-end auth and authorisation through the real middleware.

`core/authz` is unit-tested against the route table, but that only proves the
policy table is right — not that the middleware consults it, resolves the right
mode, or reads the user from the database. Those were verified by hand against a
running container; this file makes them a regression test.

Everything goes through `TestClient`, so the middleware stack runs exactly as it
does in production, including the lifespan that opens or closes setup.
"""
from __future__ import annotations

import pytest

pytest.importorskip("aiosqlite")
pytest.importorskip("cryptography")

from starlette.testclient import TestClient

from app.core import users as users_core


@pytest.fixture
def client(clean_users, monkeypatch):
    """A client on an app with no accounts and no shared password (open mode)."""
    monkeypatch.delenv("ANSIBLE_GUI_PASSWORD", raising=False)
    from app.main import app
    with TestClient(app) as c:
        yield c


def _login(client, username, password):
    r = client.post("/login", data={"username": username, "password": password},
                    follow_redirects=False)
    assert r.status_code == 303, f"login failed: {r.status_code}"
    return r


# --- open mode (no accounts, no password) ------------------------------------

def test_open_instance_needs_no_session(client):
    assert client.get("/api/projects").status_code == 200


def test_health_is_public(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


# --- single-password mode ----------------------------------------------------

def test_shared_password_blocks_api_without_a_cookie(clean_users, monkeypatch):
    monkeypatch.setenv("ANSIBLE_GUI_PASSWORD", "letmein")
    from app.main import app
    with TestClient(app) as c:
        assert c.get("/api/projects").status_code == 401


def test_shared_password_redirects_pages_to_login(clean_users, monkeypatch):
    monkeypatch.setenv("ANSIBLE_GUI_PASSWORD", "letmein")
    from app.main import app
    with TestClient(app) as c:
        r = c.get("/projects", follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == "/login"


def test_shared_password_grants_full_access(clean_users, monkeypatch):
    monkeypatch.setenv("ANSIBLE_GUI_PASSWORD", "letmein")
    from app.main import app
    with TestClient(app) as c:
        r = c.post("/login", data={"password": "letmein"}, follow_redirects=False)
        assert r.status_code == 303
        # No account exists to carry a role, so this session is unrestricted.
        assert c.get("/api/credentials").status_code == 200
        assert c.get("/api/users").status_code == 200


def test_wrong_shared_password_is_401(clean_users, monkeypatch):
    monkeypatch.setenv("ANSIBLE_GUI_PASSWORD", "letmein")
    from app.main import app
    with TestClient(app) as c:
        assert c.post("/login", data={"password": "nope"}).status_code == 401


# --- multi-user mode ---------------------------------------------------------

@pytest.fixture
async def accounts(clean_users):
    await users_core.create("boss", "admin-pw", users_core.ADMIN)
    await users_core.create("ops", "ops-pw", users_core.OPERATOR)
    await users_core.create("watcher", "view-pw", users_core.VIEWER)


@pytest.fixture
def app_client(accounts, monkeypatch):
    monkeypatch.delenv("ANSIBLE_GUI_PASSWORD", raising=False)
    from app.main import app
    with TestClient(app) as c:
        yield c


def test_accounts_switch_the_app_out_of_open_mode(app_client):
    """Creating the first account closes the door without a restart."""
    assert app_client.get("/api/projects").status_code == 401


def test_login_with_an_account(app_client):
    _login(app_client, "boss", "admin-pw")
    assert app_client.get("/api/projects").status_code == 200


def test_wrong_password_does_not_create_a_session(app_client):
    assert app_client.post("/login", data={"username": "boss", "password": "wrong"}).status_code == 401
    assert app_client.get("/api/projects").status_code == 401


def test_unknown_username_gives_the_same_answer_as_a_wrong_password(app_client):
    """The message must not reveal which accounts exist."""
    a = app_client.post("/login", data={"username": "boss", "password": "wrong"})
    b = app_client.post("/login", data={"username": "ghost", "password": "wrong"})
    assert a.status_code == b.status_code == 401
    assert "Wrong username or password" in a.text
    assert "Wrong username or password" in b.text


@pytest.mark.parametrize("who,pw,path,expected", [
    ("boss", "admin-pw", "/api/users", 200),
    ("ops", "ops-pw", "/api/users", 403),
    ("watcher", "view-pw", "/api/users", 403),
    ("boss", "admin-pw", "/api/projects", 200),
    ("ops", "ops-pw", "/api/projects", 200),
    ("watcher", "view-pw", "/api/projects", 200),
])
def test_role_decides_what_a_session_may_read(app_client, who, pw, path, expected):
    _login(app_client, who, pw)
    assert app_client.get(path).status_code == expected


@pytest.mark.parametrize("who,pw,expected", [
    ("boss", "admin-pw", 200),
    ("ops", "ops-pw", 403),
    ("watcher", "view-pw", 403),
])
def test_only_admins_may_write_credentials(app_client, who, pw, expected):
    _login(app_client, who, pw)
    r = app_client.post("/api/credentials",
                        json={"kind": "ssh_key", "name": f"k-{who}", "secret": "x"})
    assert r.status_code == expected


def test_a_denied_request_says_which_role_was_refused(app_client):
    _login(app_client, "watcher", "view-pw")
    r = app_client.get("/api/users")
    assert r.status_code == 403
    assert "viewer" in r.json()["detail"]


def test_pages_are_not_capability_checked_only_the_api_is(app_client):
    """A viewer opening /users gets the page; the data behind it is what's denied.
    Keeps the UI from having to special-case every nav item."""
    _login(app_client, "watcher", "view-pw")
    assert app_client.get("/users").status_code == 200
    assert app_client.get("/api/users").status_code == 403


# --- revocation --------------------------------------------------------------

async def test_disabling_an_account_ends_its_session(app_client):
    """The user is re-read per request, so this must not wait for token expiry."""
    _login(app_client, "ops", "ops-pw")
    assert app_client.get("/api/projects").status_code == 200

    ops = [u for u in await users_core.list_users() if u.username == "ops"][0]
    await users_core.set_disabled(ops.id, True)

    assert app_client.get("/api/projects").status_code == 401


async def test_changing_a_role_takes_effect_on_the_next_request(app_client):
    _login(app_client, "watcher", "view-pw")
    assert app_client.get("/api/users").status_code == 403

    watcher = [u for u in await users_core.list_users() if u.username == "watcher"][0]
    await users_core.set_role(watcher.id, users_core.ADMIN)

    assert app_client.get("/api/users").status_code == 200


async def test_deleting_an_account_ends_its_session(app_client):
    _login(app_client, "ops", "ops-pw")
    ops = [u for u in await users_core.list_users() if u.username == "ops"][0]

    await users_core.delete(ops.id)

    assert app_client.get("/api/projects").status_code == 401


# --- cookies -----------------------------------------------------------------

def test_session_cookie_is_httponly_and_samesite(app_client):
    r = app_client.post("/login", data={"username": "boss", "password": "admin-pw"},
                        follow_redirects=False)
    cookie = r.headers["set-cookie"]
    assert "HttpOnly" in cookie
    assert "SameSite=lax" in cookie.lower() or "samesite=lax" in cookie.lower()


def test_a_tampered_cookie_is_rejected(app_client):
    _login(app_client, "boss", "admin-pw")
    from app.core.auth import SESSION_COOKIE
    app_client.cookies.set(SESSION_COOKIE, "9999999999.1.forgedsignature")

    assert app_client.get("/api/projects").status_code == 401


def test_a_cookie_claiming_another_user_id_is_rejected(app_client):
    """The id is inside the signed payload, so it can't be edited."""
    from app.core.auth import SESSION_COOKIE
    r = app_client.post("/login", data={"username": "watcher", "password": "view-pw"},
                        follow_redirects=False)
    token = r.cookies[SESSION_COOKIE] if SESSION_COOKIE in r.cookies else None
    if token is None:  # cookie already stored on the client
        token = app_client.cookies[SESSION_COOKIE]
    exp, uid, sig = token.split(".")
    app_client.cookies.set(SESSION_COOKIE, f"{exp}.{int(uid) - 1}.{sig}")

    assert app_client.get("/api/projects").status_code == 401


def test_logout_clears_the_session(app_client):
    _login(app_client, "boss", "admin-pw")
    app_client.post("/logout", follow_redirects=False)
    assert app_client.get("/api/projects").status_code == 401


# --- security headers --------------------------------------------------------

def test_security_headers_are_present_on_an_api_response(client):
    h = client.get("/api/projects").headers
    assert h["x-frame-options"] == "DENY"
    assert h["x-content-type-options"] == "nosniff"
    assert "frame-ancestors 'none'" in h["content-security-policy"]


def test_security_headers_are_present_on_a_denied_response(app_client):
    h = app_client.get("/api/projects").headers
    assert h["x-content-type-options"] == "nosniff"
