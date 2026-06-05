"""Unit tests for `runner.summarize` (pure stats → UI summary)."""
from __future__ import annotations

from app.core.runner import RunResult, summarize


def _result(status="successful", rc=0, stats=None, failures=None):
    return RunResult(status=status, rc=rc, stats=stats or {}, failures=failures or [],
                     artifacts_dir="/tmp/x")


def test_summary_all_ok():
    res = _result(stats={"ok": {"web1": 3}, "changed": {"web1": 1}})
    out = summarize(res)
    assert out["overall"] == "ok"
    assert out["hosts"]["web1"] == {"ok": 3, "changed": 1}


def test_summary_failed_when_host_has_failures():
    res = _result(status="failed", rc=2,
                  stats={"ok": {"web1": 1}, "failures": {"web1": 1}},
                  failures=[{"host": "web1", "task": "boom"}])
    out = summarize(res)
    assert out["overall"] == "failed"
    assert out["hosts"]["web1"]["failures"] == 1
    assert out["failures"][0]["task"] == "boom"


def test_summary_failed_on_unreachable():
    res = _result(status="failed", stats={"unreachable": {"db1": 1}})
    assert summarize(res)["overall"] == "failed"


def test_summary_failed_when_runner_status_bad_even_without_host_stats():
    # Pre-task errors (e.g. role not found) leave empty host stats but a bad status.
    res = _result(status="failed", rc=1, stats={})
    assert summarize(res)["overall"] == "failed"


def test_summary_merges_multiple_buckets_per_host():
    res = _result(stats={"ok": {"h": 2}, "skipped": {"h": 5}, "changed": {"h": 1}})
    out = summarize(res)
    assert out["hosts"]["h"] == {"ok": 2, "skipped": 5, "changed": 1}
