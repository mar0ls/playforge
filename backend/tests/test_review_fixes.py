"""Regression coverage for v0.0.2 review fixes (pure helpers + API wiring)."""
from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("aiosqlite")
pytest.importorskip("cryptography")

from fastapi import HTTPException

from app.api import runs as runs_api
from app.api.runs import RunIn, start_run
from app.core import agent, ai, credentials as cred_store, scheduler, storage
from app.core.runner import RunResult, _cfg_declares, _project_envvars
from app.core.config import settings as _cfg
from app.models.db import Project, SessionLocal, init_db


# ---- runner: don't override ansible.cfg roles_path / collections_path ----

def test_cfg_declares_detects_value():
    cfg = "[defaults]\nroles_path = custom_roles\nhost_key_checking = False\n"
    assert _cfg_declares(cfg, "roles_path") is True
    assert _cfg_declares(cfg, "collections_path") is False


def test_cfg_declares_ignores_comments_and_sections():
    cfg = "# roles_path = no\n[defaults]\n; collections_path = nope\n"
    assert _cfg_declares(cfg, "roles_path") is False
    assert _cfg_declares(cfg, "collections_path") is False


def test_project_envvars_respects_user_roles_path(tmp_path):
    (tmp_path / "ansible.cfg").write_text("[defaults]\nroles_path = custom_roles\n")
    (tmp_path / "roles").mkdir()
    env = _project_envvars(tmp_path)
    # roles/ exists, but cfg already declares roles_path → must NOT set the env var
    assert "ANSIBLE_ROLES_PATH" not in env
    assert env.get("ANSIBLE_CONFIG", "").endswith("ansible.cfg")


def test_project_envvars_fills_in_when_cfg_silent(tmp_path):
    (tmp_path / "ansible.cfg").write_text("[defaults]\nstdout_callback = yaml\n")
    (tmp_path / "roles").mkdir()
    env = _project_envvars(tmp_path)
    assert env["ANSIBLE_ROLES_PATH"] == str(tmp_path / "roles")


# ---- storage: new project scaffold no longer hard-codes host_key_checking=False ----

async def test_new_project_does_not_disable_host_key_check():
    await init_db()
    paths = storage.create_project("hk-test")
    try:
        cfg = (paths.root / "ansible.cfg").read_text()
        # The line must exist commented out so users see it as an option without
        # silently inheriting an insecure default.
        assert "# host_key_checking" in cfg
        assert _cfg_declares(cfg, "host_key_checking") is False
    finally:
        storage.delete_project(paths.project_id)


# ---- storage: .git/ rejection (already-fixed regression, kept tight) ----

def test_resolve_safe_blocks_git_writes(tmp_path):
    pp = tmp_path
    with pytest.raises(storage.StorageError):
        storage._resolve_safe(pp, ".git/hooks/post-commit")


# ---- storage: per-file history + at-sha ----

async def test_file_history_returns_commits():
    await init_db()
    paths = storage.create_project("hist-test")
    try:
        # create_project commits an example site.yml; we add two more revisions.
        v1 = "---\n- hosts: all\n  tasks:\n    - name: v1\n      ansible.builtin.ping:\n"
        v2 = "---\n- hosts: all\n  tasks:\n    - name: v2\n      ansible.builtin.ping:\n"
        storage.write_file(paths.project_id, "playbooks/site.yml", v1, "first edit")
        storage.write_file(paths.project_id, "playbooks/site.yml", v2, "second edit")

        commits = storage.file_history(paths.project_id, "playbooks/site.yml")
        assert len(commits) >= 3   # scaffold + first edit + second edit
        assert commits[0]["message"]   # most recent first
        # file_at on the previous version returns the OLD content, distinct from HEAD.
        prev_sha = commits[1]["sha"]
        prev = storage.file_at(paths.project_id, "playbooks/site.yml", prev_sha)
        assert "v1" in prev and "v2" not in prev
    finally:
        storage.delete_project(paths.project_id)


# ---- agent: loop guard is now THREE strikes ----

async def test_agent_loop_guard_threshold():
    from app.core.agent import Tool, READ, run_agent

    calls = {"n": 0}

    def t_run(_args: dict) -> dict:
        calls["n"] += 1
        return {"observation": "stuck"}

    tools = {"poke": Tool("poke", READ, "x", t_run)}

    # Model emits the same poke action every turn — should be tolerated TWICE
    # before being cut (i.e. allow the third call before the loop guard fires).
    def chat_fn(_system: str, _msgs: list[dict]) -> str:
        return '```action\n{"tool": "poke", "args": {"k": 1}}\n```'

    result = await run_agent("goal", tools, chat_fn=chat_fn,
                              allowed_levels={READ}, max_steps=10)
    # Loop-guard semantics: stop on the third identical action.
    # So we should have run it twice, then refused the third → calls == 2.
    assert calls["n"] == 2
    assert "loop" in result.stopped_reason


# ---- runs API: unknown project → 404, not silently 'failed' Run row ----

async def test_start_run_unknown_project_404():
    await init_db()
    with pytest.raises(HTTPException) as exc:
        await start_run(RunIn(project_id="this-id-does-not-exist", playbook="site.yml"))
    assert exc.value.status_code == 404


# ---- credentials: secret_path retired, has_secret returns bool ----

def test_credentials_has_secret_bool_only():
    # The function returns a bool; not a Path. Callers can't accidentally hand an
    # encrypted file path to ssh -i / --private-key.
    out = cred_store.has_secret(999999)  # non-existent id
    assert out is False
    assert isinstance(out, bool)


# ---- ai: chat cache can be cleared on collection change ----

def test_clear_chat_cache_drops_entries():
    ai._CHAT_CACHE["x"] = {"reply": "from-stale-corpus"}
    ai._CHAT_CACHE["y"] = {"reply": "another"}
    ai.clear_chat_cache()
    assert ai._CHAT_CACHE == {}


# ---- scheduler: per-schedule timezone validation ----

def test_validate_timezone_accepts_iana():
    assert scheduler.validate_timezone("Europe/Warsaw") == "Europe/Warsaw"
    assert scheduler.validate_timezone("") == ""  # = UTC fallback


def test_validate_timezone_rejects_garbage():
    with pytest.raises(ValueError):
        scheduler.validate_timezone("Mars/Olympus_Mons")


def test_next_fire_iso_respects_timezone():
    # Same cron, different tz → different next-fire wall time. The iso strings
    # must reflect the local offset (not be silently UTC).
    iso_warsaw = scheduler.next_fire_iso("0 12 * * *", tz="Europe/Warsaw") or ""
    iso_utc = scheduler.next_fire_iso("0 12 * * *", tz="") or ""
    assert iso_warsaw and iso_utc
    # Warsaw is ahead of UTC year-round (+01 or +02), so "12:00 Warsaw" is sooner
    # in absolute terms than "12:00 UTC" on the same date → its iso starts with a
    # different hour-of-day component (or carries a +HH:MM suffix).
    assert iso_warsaw != iso_utc


# ---- runner: _EventCollector normalizes failures the same way for both paths ----

def test_event_collector_clears_rescued_on_success():
    from app.core.runner import _EventCollector
    c = _EventCollector()
    c.capture({"event": "runner_on_failed",
               "event_data": {"host": "h1", "task": "t", "res": {"msg": "boom"}}})
    # Successful overall = rescued/ignored → must NOT surface as actionable failure
    assert c.finalize("successful") == []
    # But on a real failure with the same events captured, they survive.
    assert c.finalize("failed") == [{"host": "h1", "task": "t",
                                      "result": {"msg": "boom"}, "stderr": ""}]


def test_event_collector_synthesizes_pre_task_error_on_dead_run():
    from app.core.runner import _EventCollector
    c = _EventCollector()
    c.capture({"event": "verbose", "stdout": "ERROR! the playbook is broken"})
    out = c.finalize("failed")
    assert len(out) == 1
    assert out[0]["task"] == "(pre-task error)"
    assert "broken" in out[0]["result"]["msg"]


# ---- security: WebSocket run endpoint re-checks auth (HTTP middleware skips WS) ----

class _FakeWS:
    """Minimal WebSocket stand-in: records accept()/close() and serves cookies.

    `headers` defaults to same-origin because a real handshake always carries
    them and the handler now checks Origin before anything else. These tests are
    about the auth decision, so they hand it a request that passes that check —
    the cross-site case has its own tests in test_csrf.py.
    """
    def __init__(self, cookies=None, headers=None):
        self.cookies = cookies or {}
        self.headers = headers if headers is not None else {"host": "testserver"}
        self.accepted = False
        self.closed_code = None
    async def accept(self):
        self.accepted = True
    async def close(self, code=1000):
        self.closed_code = code
    async def receive_json(self):
        # Only reached if auth passed; abort cleanly so the test doesn't run a playbook.
        raise RuntimeError("should not get here in the rejection test")


async def test_ws_rejects_unauthenticated_when_password_set(monkeypatch):
    from app.api.runs import run_ws
    from app.core import auth
    monkeypatch.setattr(auth, "auth_enabled", lambda: True)
    monkeypatch.setattr(auth, "verify_token", lambda tok: False)  # no/invalid cookie
    ws = _FakeWS(cookies={})
    await run_ws(ws)
    assert ws.closed_code == 4401      # rejected with app-range unauthorized
    assert ws.accepted is False        # and never accepted → no playbook can run


async def test_ws_allows_open_instance(monkeypatch):
    # Auth disabled (no password) → WS must still work (single-user local mode).
    from app.api.runs import run_ws
    from app.core import auth
    monkeypatch.setattr(auth, "auth_enabled", lambda: False)
    ws = _FakeWS(cookies={})
    with pytest.raises(RuntimeError):   # gets past the guard, into receive_json()
        await run_ws(ws)
    assert ws.accepted is True
