"""Golden-set regression harness for the self-checking layers.

Cases live as data in tests/golden/*.yml (see the README there). They pin the
behaviour of the rule engine and the module validator — the two things that make
AI output trustworthy — with no model in the loop, so a regression here is
unambiguously ours and not the LLM's.

Assertions match the stable `rule` id, never message text.
"""
from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("yaml")
import yaml

from app.core import playbook_rules

GOLDEN_DIR = Path(__file__).resolve().parent / "golden"


def _load_cases() -> list[dict]:
    cases = []
    for path in sorted(GOLDEN_DIR.glob("*.yml")):
        data = yaml.safe_load(path.read_text())
        data["_file"] = path.name
        cases.append(data)
    return cases


CASES = _load_cases()
assert CASES, "no golden cases found — did tests/golden/ move?"


def _ids(findings: list[dict]) -> list[str]:
    return [f["rule"] for f in findings]


@pytest.mark.parametrize("case", CASES, ids=[c["_file"] for c in CASES])
def test_rule_expectations(case):
    findings = playbook_rules.check_text(case["content"])
    fired = _ids(findings)

    for rule in case.get("expect_rules", []):
        assert rule in fired, (
            f"{case['_file']}: expected rule {rule!r} to fire, got {sorted(set(fired))}"
        )

    for rule in case.get("expect_absent", []):
        assert rule not in fired, (
            f"{case['_file']}: rule {rule!r} fired but must not — false positive.\n"
            + "\n".join(f["message"] for f in findings if f["rule"] == rule)
        )

    # An empty expect_rules means "this playbook is clean": no rule may fire at
    # all. Without this, a case could silently stop asserting anything.
    if case.get("expect_rules") == [] and "expect_invalid_modules" not in case:
        assert fired == [], (
            f"{case['_file']}: expected a clean playbook, got "
            + "; ".join(f"{f['rule']}: {f['message']}" for f in findings)
        )


@pytest.mark.parametrize(
    "case",
    [c for c in CASES if "expect_invalid_modules" in c or "expect_uninstalled_modules" in c],
    ids=[c["_file"] for c in CASES
         if "expect_invalid_modules" in c or "expect_uninstalled_modules" in c],
)
def test_module_validator_expectations(case):
    """Needs ansible-doc and the baked collections — image only."""
    from app.core.ai_validate import validate_text

    result = validate_text(case["content"])
    if not result.get("checked_modules"):
        pytest.skip("ansible-doc unavailable (running outside the image)")

    for mod in case.get("expect_invalid_modules", []):
        assert mod in result["invalid_modules"], (
            f"{case['_file']}: {mod!r} should be reported invalid, "
            f"got invalid={result['invalid_modules']} uninstalled={result['uninstalled_modules']}"
        )
    for mod in case.get("expect_uninstalled_modules", []):
        assert mod in result["uninstalled_modules"], (
            f"{case['_file']}: {mod!r} should be reported uninstalled, "
            f"got {result['uninstalled_modules']}"
        )


def test_every_case_declares_why():
    """A case nobody can explain is a case nobody can maintain."""
    for case in CASES:
        assert case.get("name"), f"{case['_file']}: missing name"
        assert case.get("why", "").strip(), f"{case['_file']}: missing 'why'"


def test_cases_only_reference_real_rules():
    """Catches a typo'd rule id, which would otherwise make a case assert nothing."""
    for case in CASES:
        for key in ("expect_rules", "expect_absent"):
            for rule in case.get(key, []):
                assert rule in playbook_rules.ALL_RULES, (
                    f"{case['_file']}: unknown rule id {rule!r} in {key}. "
                    f"Known: {sorted(playbook_rules.ALL_RULES)}"
                )


def test_golden_set_exercises_every_rule():
    """Every rule the engine can emit must have at least one case.

    This is what stops the golden set rotting: add a rule without a case and the
    suite says so.
    """
    covered = {r for c in CASES for r in c.get("expect_rules", [])}
    missing = playbook_rules.ALL_RULES - covered
    assert not missing, (
        "rules with no golden case: " + ", ".join(sorted(missing))
        + " — add one to tests/golden/"
    )
