"""First-admin bootstrap: environment variables and the token-gated setup page.

The threat this guards against is concrete: docker-compose.yml publishes the port
on 0.0.0.0, so between `docker compose up -d` and the operator opening a browser,
an unguarded setup page would let whoever reached it first claim the instance.
"""
from __future__ import annotations

import pytest

pytest.importorskip("aiosqlite")
pytest.importorskip("cryptography")

from app.core import bootstrap, users
from app.models.db import SessionLocal, User, init_db


@pytest.fixture(autouse=True)
async def _clean(monkeypatch, clean_users):
    bootstrap.clear_setup_token()
    for var in ("ANSIBLE_GUI_ADMIN_USER", "ANSIBLE_GUI_ADMIN_PASSWORD",
                "ANSIBLE_GUI_ADMIN_USER_FILE", "ANSIBLE_GUI_ADMIN_PASSWORD_FILE",
                "ANSIBLE_GUI_PASSWORD"):
        monkeypatch.delenv(var, raising=False)
    yield
    bootstrap.clear_setup_token()


# --- environment bootstrap ---------------------------------------------------

async def test_env_bootstrap_creates_an_admin(monkeypatch):
    monkeypatch.setenv("ANSIBLE_GUI_ADMIN_USER", "marcin")
    monkeypatch.setenv("ANSIBLE_GUI_ADMIN_PASSWORD", "s3cret")

    assert await bootstrap.apply_env_bootstrap() is True

    user = await users.authenticate("marcin", "s3cret")
    assert user is not None and user.role == users.ADMIN


async def test_env_bootstrap_is_a_noop_without_both_variables(monkeypatch):
    monkeypatch.setenv("ANSIBLE_GUI_ADMIN_USER", "marcin")

    assert await bootstrap.apply_env_bootstrap() is False
    assert await users.count() == 0


async def test_env_bootstrap_does_not_touch_an_existing_install(monkeypatch):
    """Restarting the container must not reset a password someone has changed."""
    await users.create("marcin", "the-real-password", users.ADMIN)
    monkeypatch.setenv("ANSIBLE_GUI_ADMIN_USER", "marcin")
    monkeypatch.setenv("ANSIBLE_GUI_ADMIN_PASSWORD", "from-the-env")

    assert await bootstrap.apply_env_bootstrap() is False

    assert await users.authenticate("marcin", "the-real-password") is not None
    assert await users.authenticate("marcin", "from-the-env") is None


async def test_env_bootstrap_reads_a_secrets_file(monkeypatch, tmp_path):
    """The `_FILE` form is what lets a docker/k8s secret supply the password
    instead of it sitting in .env."""
    pw = tmp_path / "admin_password"
    pw.write_text("from-a-secret\n")
    monkeypatch.setenv("ANSIBLE_GUI_ADMIN_USER", "marcin")
    monkeypatch.setenv("ANSIBLE_GUI_ADMIN_PASSWORD_FILE", str(pw))

    assert await bootstrap.apply_env_bootstrap() is True
    assert await users.authenticate("marcin", "from-a-secret") is not None


async def test_unreadable_secrets_file_does_not_create_an_account(monkeypatch, tmp_path):
    monkeypatch.setenv("ANSIBLE_GUI_ADMIN_USER", "marcin")
    monkeypatch.setenv("ANSIBLE_GUI_ADMIN_PASSWORD_FILE", str(tmp_path / "missing"))

    assert await bootstrap.apply_env_bootstrap() is False
    assert await users.count() == 0


async def test_env_bootstrap_rejects_an_invalid_username(monkeypatch, caplog):
    monkeypatch.setenv("ANSIBLE_GUI_ADMIN_USER", "has space")
    monkeypatch.setenv("ANSIBLE_GUI_ADMIN_PASSWORD", "s3cret")

    with caplog.at_level("ERROR"):
        assert await bootstrap.apply_env_bootstrap() is False

    assert await users.count() == 0
    assert "did not create an account" in caplog.text


# --- setup token -------------------------------------------------------------

def test_no_token_until_one_is_issued():
    assert bootstrap.setup_token() is None
    assert bootstrap.check_setup_token("anything") is False


def test_issued_token_verifies():
    token = bootstrap.issue_setup_token()
    assert bootstrap.check_setup_token(token) is True


def test_wrong_token_is_rejected():
    bootstrap.issue_setup_token()
    assert bootstrap.check_setup_token("not-the-token") is False


def test_empty_token_is_rejected_even_when_setup_is_open():
    bootstrap.issue_setup_token()
    assert bootstrap.check_setup_token("") is False


def test_token_changes_every_time_it_is_issued():
    """Regenerated per process, so it can't be replayed from an old log."""
    assert bootstrap.issue_setup_token() != bootstrap.issue_setup_token()


def test_token_is_long_enough_to_resist_guessing():
    assert len(bootstrap.issue_setup_token()) >= 32


def test_clearing_closes_setup():
    token = bootstrap.issue_setup_token()
    bootstrap.clear_setup_token()

    assert bootstrap.setup_token() is None
    assert bootstrap.check_setup_token(token) is False


# --- prepare() decides which route applies -----------------------------------

async def test_prepare_opens_setup_on_a_blank_install():
    await bootstrap.prepare()
    assert bootstrap.setup_token() is not None


async def test_prepare_does_not_open_setup_when_accounts_exist():
    await users.create("marcin", "pw", users.ADMIN)
    await bootstrap.prepare()
    assert bootstrap.setup_token() is None


async def test_prepare_does_not_open_setup_in_single_password_mode(monkeypatch):
    """A legacy install is already reachable by its operator; moving it to
    accounts is their decision, not a first boot's."""
    monkeypatch.setenv("ANSIBLE_GUI_PASSWORD", "legacy")

    await bootstrap.prepare()

    assert bootstrap.setup_token() is None


async def test_prepare_prefers_the_environment_over_the_setup_page(monkeypatch):
    monkeypatch.setenv("ANSIBLE_GUI_ADMIN_USER", "marcin")
    monkeypatch.setenv("ANSIBLE_GUI_ADMIN_PASSWORD", "s3cret")

    await bootstrap.prepare()

    assert bootstrap.setup_token() is None
    assert await users.count() == 1
