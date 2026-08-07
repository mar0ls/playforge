"""Tests for the concrete agent tool set (bound to a real tmp project)."""
from __future__ import annotations

import shutil
import pytest

pytest.importorskip("git")
if shutil.which("git") is None:
    pytest.skip("git binary not available", allow_module_level=True)

from app.core import storage
from app.core.agent_tools import build_tools


def _proj():
    return storage.create_project("agenttools")


def test_read_and_tree():
    p = _proj()
    try:
        storage.write_file(p.project_id, "playbooks/site.yml", "- hosts: all\n")
        tools = build_tools(p.project_id)
        assert "site.yml" in tools["list_tree"].run({})["tree"]["playbooks"]
        assert "hosts: all" in tools["read_file"].run({"path": "playbooks/site.yml"})["content"]
    finally:
        storage.delete_project(p.project_id)


def test_write_autofixes_and_lints():
    p = _proj()
    try:
        tools = build_tools(p.project_id)
        # bare jinja → autofixed on write
        obs = tools["write_file"].run({"path": "playbooks/x.yml",
              "content": "- hosts: all\n  tasks:\n    - name: t\n      ansible.builtin.file:\n        owner: {{ u }}\n        path: /tmp/x\n"})
        saved = storage.read_file(p.project_id, "playbooks/x.yml")
        assert 'owner: "{{ u }}"' in saved   # quoted by autofix
        assert obs["saved"] == "playbooks/x.yml"
    finally:
        storage.delete_project(p.project_id)


def test_move_and_mkdir():
    p = _proj()
    try:
        tools = build_tools(p.project_id)
        tools["mkdir"].run({"path": "group_vars/prod"})
        storage.write_file(p.project_id, "tmp.yml", "x: 1\n")
        obs = tools["move"].run({"src": "tmp.yml", "dst": "group_vars/prod/main.yml"})
        assert obs["to"] == "group_vars/prod/main.yml"
        assert (p.root / "group_vars" / "prod" / "main.yml").is_file()
    finally:
        storage.delete_project(p.project_id)


def test_lint_playbook_reports_issues():
    p = _proj()
    try:
        tools = build_tools(p.project_id)
        obs = tools["lint_playbook"].run({"content": "- tasks: []\n"})  # missing hosts
        assert any("hosts" in i["message"] for i in obs["issues"])
    finally:
        storage.delete_project(p.project_id)


def test_get_run_injected():
    p = _proj()
    try:
        tools = build_tools(p.project_id, get_run=lambda rid: {"id": rid, "status": "failed"})
        assert tools["get_run"].run({"run_id": 5})["status"] == "failed"
        # without injection → graceful error
        t2 = build_tools(p.project_id)
        assert "error" in t2["get_run"].run({"run_id": 5})
    finally:
        storage.delete_project(p.project_id)


def test_web_fetch_domain_allowlist():
    p = _proj()
    try:
        tools = build_tools(p.project_id)
        bad = tools["web_fetch"].run({"url": "http://evil.example.com/x", "module": "x"})
        assert "not allowed" in bad["error"]
        # no module name → refuses arbitrary scraping
        none = tools["web_fetch"].run({"module": ""})
        assert "error" in none
    finally:
        storage.delete_project(p.project_id)


def test_tool_levels():
    p = _proj()
    try:
        tools = build_tools(p.project_id)
        from app.core.agent import READ, MUTATE, CONFIRM
        assert tools["read_file"].level == READ
        assert tools["write_file"].level == MUTATE
        assert tools["delete"].level == CONFIRM
        assert tools["web_fetch"].level == CONFIRM
    finally:
        storage.delete_project(p.project_id)


def test_preview_and_run_tools(monkeypatch):
    import types
    from app.core import runner as runner_mod
    calls = []
    def _fake(req, isolation=None):
        calls.append({"check": req.check, "playbook": req.playbook, "isolation": isolation})
        return types.SimpleNamespace(status="successful", rc=0, failures=[], changes=[], stats={})
    monkeypatch.setattr(runner_mod, "run_playbook_sync", _fake)
    p = _proj()
    try:
        tools = build_tools(p.project_id)
        from app.core.agent import MUTATE, CONFIRM
        assert tools["preview"].level == MUTATE
        assert tools["run_playbook"].level == CONFIRM
        out = tools["preview"].run({"playbook": "playbooks/site.yml"})
        assert out["check"] is True and out["status"] == "successful"
        assert calls[0]["check"] is True
        tools["run_playbook"].run({"playbook": "playbooks/site.yml"})
        assert calls[1]["check"] is False     # real run
    finally:
        storage.delete_project(p.project_id)


def test_preview_surfaces_failures(monkeypatch):
    import types
    from app.core import runner as runner_mod
    monkeypatch.setattr(runner_mod, "run_playbook_sync",
        lambda req, isolation=None: types.SimpleNamespace(status="failed", rc=2, changes=[], stats={},
            failures=[{"host": "vps1", "task": "install", "result": {"msg": "No package matching"}}]))
    p = _proj()
    try:
        tools = build_tools(p.project_id)
        out = tools["preview"].run({"playbook": "playbooks/site.yml"})
        assert out["status"] == "failed"
        assert out["failures"][0]["host"] == "vps1"
        assert "No package" in out["failures"][0]["msg"]
    finally:
        storage.delete_project(p.project_id)
