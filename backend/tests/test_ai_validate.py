"""Unit tests for the deterministic anti-hallucination layer."""
from __future__ import annotations

from app.core import ai_validate
from app.core.ai_validate import downgrade_confidence, validate_text


# --- downgrade_confidence ---------------------------------------------------

def test_downgrade_two_claims_to_low():
    v = {"confidence": "high"}
    downgrade_confidence(v, unsupported_claims=2)
    assert v["confidence"] == "low"


def test_downgrade_one_claim_high_to_medium():
    v = {"confidence": "high"}
    downgrade_confidence(v, unsupported_claims=1)
    assert v["confidence"] == "medium"


def test_downgrade_one_claim_keeps_low():
    v = {"confidence": "low"}
    downgrade_confidence(v, unsupported_claims=1)
    assert v["confidence"] == "low"


def test_downgrade_zero_claims_noop():
    v = {"confidence": "high"}
    downgrade_confidence(v, unsupported_claims=0)
    assert v["confidence"] == "high"


# --- validate_text (known_modules mocked) -----------------------------------

def test_invalid_builtin_module_lowers_confidence(monkeypatch):
    # ansible.builtin is always complete, so a missing builtin is a real error.
    monkeypatch.setattr(ai_validate, "known_modules",
                        lambda: frozenset({"ansible.builtin.apt", "ansible.builtin.ping"}))
    out = validate_text("Use ansible.builtin.apt and ansible.builtin.ufw to set up the firewall.")
    assert out["checked_modules"] is True
    assert out["invalid_modules"] == ["ansible.builtin.ufw"]
    assert out["uninstalled_modules"] == []
    assert out["confidence"] == "medium"  # exactly one invalid builtin


def test_two_invalid_builtins_is_low(monkeypatch):
    monkeypatch.setattr(ai_validate, "known_modules", lambda: frozenset({"ansible.builtin.apt"}))
    out = validate_text("try ansible.builtin.nope and ansible.builtin.alsonope")
    assert len(out["invalid_modules"]) == 2
    assert out["confidence"] == "low"


def test_platform_builtin_not_flagged_as_invalid(monkeypatch):
    # ansible.builtin.yum is real but ansible-doc -l doesn't list it on a Debian
    # image. It must NOT be called "doesn't exist" (was a misleading message).
    monkeypatch.setattr(ai_validate, "known_modules", lambda: frozenset({"ansible.builtin.apt"}))
    out = validate_text("use ansible.builtin.yum to install the package")
    assert out["invalid_modules"] == []
    assert out["uninstalled_modules"] == []
    assert out["confidence"] == "high"


def test_genuinely_fake_builtin_still_flagged(monkeypatch):
    monkeypatch.setattr(ai_validate, "known_modules", lambda: frozenset({"ansible.builtin.apt"}))
    out = validate_text("use ansible.builtin.ufw for the firewall")  # ufw is NOT a builtin
    assert out["invalid_modules"] == ["ansible.builtin.ufw"]
    assert out["confidence"] == "medium"


def test_uninstalled_collection_is_not_a_hallucination(monkeypatch):
    # community.general.ufw and ansible.posix.authorized_key are REAL modules; if the
    # collection just isn't installed here, confidence must stay high (actionable note,
    # not "not found"). This was the bug a user hit on a hardening playbook.
    monkeypatch.setattr(ai_validate, "known_modules", lambda: frozenset({"ansible.builtin.apt"}))
    out = validate_text("uses community.general.ufw and ansible.posix.authorized_key")
    assert out["invalid_modules"] == []
    assert set(out["uninstalled_modules"]) == {"community.general.ufw", "ansible.posix.authorized_key"}
    assert out["confidence"] == "high"


def test_mixed_invalid_and_uninstalled(monkeypatch):
    monkeypatch.setattr(ai_validate, "known_modules", lambda: frozenset({"ansible.builtin.apt"}))
    out = validate_text("ansible.builtin.ufw plus community.general.ufw")
    assert out["invalid_modules"] == ["ansible.builtin.ufw"]
    assert out["uninstalled_modules"] == ["community.general.ufw"]
    assert out["confidence"] == "medium"  # one invalid builtin


def test_validate_text_skips_when_ansible_doc_unavailable(monkeypatch):
    monkeypatch.setattr(ai_validate, "known_modules", lambda: frozenset())
    out = validate_text("mentions community.general.foo")
    assert out["checked_modules"] is False
    assert out["confidence"] == "high"
    assert "note" in out


def test_validate_text_empty():
    out = validate_text("")
    assert out["confidence"] == "high"
    assert out["modules_mentioned"] == []


def test_fqdn_and_filenames_not_treated_as_modules(monkeypatch):
    # Regression from the battery run: FQDNs / filenames / dotted var access share the
    # a.b.c shape but must NOT be reported as modules.
    monkeypatch.setattr(ai_validate, "known_modules", lambda: frozenset({"ansible.builtin.copy"}))
    text = ("connect to server.example.com and smtp.example.com, copy certs.tar.gz and "
            "config.tar.gz, check backup_exists.stat.exists")
    out = validate_text(text)
    assert out["modules_mentioned"] == []
    assert out["invalid_modules"] == []
    assert out["uninstalled_modules"] == []
    assert out["confidence"] == "high"


def test_known_namespace_token_still_detected(monkeypatch):
    monkeypatch.setattr(ai_validate, "known_modules", lambda: frozenset({"ansible.builtin.copy"}))
    out = validate_text("use community.general.ufw and kubernetes.core.k8s here")
    # Both are real namespaces → recognised as (uninstalled) modules, not dropped.
    assert set(out["uninstalled_modules"]) == {"community.general.ufw", "kubernetes.core.k8s"}


def test_registered_var_dotted_access_ignored(monkeypatch):
    # `result.stat.exists` is var access, not a module (not a known namespace).
    monkeypatch.setattr(ai_validate, "known_modules", lambda: frozenset({"ansible.builtin.stat"}))
    out = validate_text("when: result.stat.exists and item.user.name is defined")
    assert out["modules_mentioned"] == []


def test_module_regex_ignores_filesystem_paths(monkeypatch):
    monkeypatch.setattr(ai_validate, "known_modules", lambda: frozenset({"ansible.builtin.copy"}))
    # `/etc/yum.repos.d/` must not be mistaken for a module reference.
    out = validate_text("Edit /etc/yum.repos.d/epel.repo then run ansible.builtin.copy")
    assert out["unknown_modules"] == []
