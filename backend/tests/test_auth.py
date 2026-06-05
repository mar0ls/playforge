"""Tests for optional password auth + signed session tokens."""
from __future__ import annotations

import time

from app.core import auth


def test_disabled_when_no_password(monkeypatch):
    monkeypatch.delenv("ANSIBLE_GUI_PASSWORD", raising=False)
    assert auth.auth_enabled() is False


def test_enabled_with_password(monkeypatch):
    monkeypatch.setenv("ANSIBLE_GUI_PASSWORD", "s3cret")
    assert auth.auth_enabled() is True


def test_check_password_constant_time(monkeypatch):
    monkeypatch.setenv("ANSIBLE_GUI_PASSWORD", "s3cret")
    assert auth.check_password("s3cret") is True
    assert auth.check_password("wrong") is False
    assert auth.check_password("") is False


def test_token_roundtrip(monkeypatch):
    monkeypatch.setenv("ANSIBLE_GUI_PASSWORD", "s3cret")
    tok = auth.issue_token()
    assert auth.verify_token(tok) is True


def test_token_rejects_tampering(monkeypatch):
    monkeypatch.setenv("ANSIBLE_GUI_PASSWORD", "s3cret")
    tok = auth.issue_token()
    exp, _, sig = tok.partition(".")
    # tamper with the expiry (extend it) → signature no longer matches
    forged = f"{int(exp) + 999999}.{sig}"
    assert auth.verify_token(forged) is False


def test_token_expires(monkeypatch):
    monkeypatch.setenv("ANSIBLE_GUI_PASSWORD", "s3cret")
    past = time.time() - auth._SESSION_TTL - 10
    tok = auth.issue_token(now=past)
    assert auth.verify_token(tok) is False


def test_token_invalid_for_different_password(monkeypatch):
    monkeypatch.setenv("ANSIBLE_GUI_PASSWORD", "s3cret")
    tok = auth.issue_token()
    monkeypatch.setenv("ANSIBLE_GUI_PASSWORD", "changed")
    # signing key derives from the password → old cookie no longer valid
    assert auth.verify_token(tok) is False


def test_verify_garbage(monkeypatch):
    monkeypatch.setenv("ANSIBLE_GUI_PASSWORD", "s3cret")
    for bad in [None, "", "nodot", "abc.def", "123.notbase64!!"]:
        assert auth.verify_token(bad) is False
