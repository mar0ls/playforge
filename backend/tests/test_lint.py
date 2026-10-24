"""Tests for core/lint.py — ansible-lint output parsing and the subprocess guards.

The real ansible-lint binary is slow and image-only, so the subprocess is mocked:
what matters here is that we scan its stdout for the JSON array, normalise each
issue into the shape the Monaco editor consumes, and fail soft when the tool is
missing, times out, or the path is bogus.
"""
from __future__ import annotations

import subprocess

import pytest

pytest.importorskip("ansible")  # lint imports runner -> ansible; skip in a bare venv

from app.core import lint


def test_normalize_maps_severity_and_positions():
    raw = {
        "check_name": "no-changed-when",
        "severity": "major",
        "description": "  Commands should not change things.  ",
        "location": {"positions": {"begin": {"line": 7, "column": 3},
                                   "end": {"line": 7, "column": 25}}},
        "url": "https://example/rule",
    }
    out = lint._normalize(raw)
    assert out["rule"] == "no-changed-when"
    assert out["monaco_severity"] == "error"       # major -> error
    assert out["line"] == 7 and out["column"] == 3
    assert out["end_line"] == 7 and out["end_column"] == 25
    assert out["description"] == "Commands should not change things."


def test_normalize_defaults_when_fields_absent():
    out = lint._normalize({})
    assert out["rule"] == "unknown"
    assert out["monaco_severity"] == "info"        # unknown severity -> info
    assert out["line"] == 1 and out["column"] == 1
    assert out["end_column"] == out["column"] + 20  # synthesised span


def _fake_proc(stdout="", stderr="", rc=0):
    return subprocess.CompletedProcess(args=[], returncode=rc, stdout=stdout, stderr=stderr)


def test_lint_file_parses_the_json_array(tmp_path, monkeypatch):
    f = tmp_path / "play.yml"
    f.write_text("---\n- hosts: all\n  tasks: []\n")
    payload = '[{"check_name":"risky-shell-pipe","severity":"minor",' \
              '"location":{"positions":{"begin":{"line":2,"column":1}}}}]'
    monkeypatch.setattr(lint.subprocess, "run",
                        lambda *a, **k: _fake_proc(stdout="noise\n" + payload + "\ntrailing", rc=2))
    res = lint.lint_file(tmp_path, "play.yml")
    assert res["rc"] == 2
    assert len(res["issues"]) == 1
    assert res["issues"][0]["rule"] == "risky-shell-pipe"
    assert res["issues"][0]["monaco_severity"] == "warning"   # minor -> warning


def test_lint_file_rejects_path_escape(tmp_path):
    with pytest.raises(ValueError):
        lint.lint_file(tmp_path, "../../etc/hosts")


def test_lint_file_on_a_non_file_is_soft(tmp_path):
    res = lint.lint_file(tmp_path, "does-not-exist.yml")
    assert res["rc"] == 0 and res["issues"] == [] and "not a file" in res["error"]


def test_lint_file_handles_missing_binary(tmp_path, monkeypatch):
    f = tmp_path / "p.yml"
    f.write_text("---\n")
    def _boom(*a, **k):
        raise FileNotFoundError
    monkeypatch.setattr(lint.subprocess, "run", _boom)
    res = lint.lint_file(tmp_path, "p.yml")
    assert res["rc"] == -1 and "not installed" in res["error"]


def test_lint_file_handles_timeout(tmp_path, monkeypatch):
    f = tmp_path / "p.yml"
    f.write_text("---\n")
    def _slow(*a, **k):
        raise subprocess.TimeoutExpired(cmd="ansible-lint", timeout=60)
    monkeypatch.setattr(lint.subprocess, "run", _slow)
    res = lint.lint_file(tmp_path, "p.yml")
    assert res["rc"] == -1 and "timed out" in res["error"]
