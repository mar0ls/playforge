"""Tests for the agent ReAct loop (no model, no network — chat_fn is faked)."""
from __future__ import annotations

import json

import pytest

from app.core import agent
from app.core.agent import Tool, READ, MUTATE, CONFIRM, run_agent


def _act(tool, **args):
    return f'```action\n{json.dumps({"tool": tool, "args": args})}\n```'


def _scripted(replies):
    """A chat_fn that returns the next canned reply each call."""
    it = iter(replies)
    return lambda system, messages: next(it)


def _tools(calls):
    return {
        "read_file": Tool("read_file", READ, "read a file",
                          lambda a: calls.append(("read_file", a)) or {"content": "data"}),
        "write_file": Tool("write_file", MUTATE, "write a file",
                           lambda a: calls.append(("write_file", a)) or {"saved": a.get("path")}),
        "delete": Tool("delete", CONFIRM, "delete",
                       lambda a: calls.append(("delete", a)) or {"deleted": a.get("path")}),
    }


async def test_extract_action_variants():
    assert agent._extract_action('```action\n{"tool":"x","args":{}}\n```')["tool"] == "x"
    assert agent._extract_action('```\n{"tool":"y"}\n```')["tool"] == "y"
    assert agent._extract_action('{"tool":"z"}')["tool"] == "z"
    assert agent._extract_action("no json here") is None


async def test_finish_returns_summary():
    res = await run_agent("do nothing", _tools([]),
                          chat_fn=_scripted([_act("finish", summary="all done")]),
                          allowed_levels={READ})
    assert res.finished and res.summary == "all done"


async def test_read_then_finish_records_steps():
    calls = []
    res = await run_agent("read it", _tools(calls), chat_fn=_scripted([
        _act("read_file", path="site.yml"),
        _act("finish", summary="read it"),
    ]), allowed_levels={READ})
    assert res.finished
    assert calls == [("read_file", {"path": "site.yml"})]
    assert res.steps[0]["observation"]["content"] == "data"


async def test_mutating_tool_blocked_when_not_allowed():
    calls = []
    res = await run_agent("write", _tools(calls), chat_fn=_scripted([
        _act("write_file", path="x.yml", content="a"),
        _act("finish", summary="tried"),
    ]), allowed_levels={READ})  # MUTATE not allowed
    assert calls == []  # write never executed
    assert "disabled" in res.steps[0]["observation"]["error"]


async def test_disabled_tools_not_advertised():
    # The system prompt must list only usable tools + a hint about disabled ones.
    captured = {}
    def chat(system, messages):
        captured["system"] = system
        return _act("finish", summary="ok")
    await run_agent("x", _tools([]), chat_fn=chat, allowed_levels={READ})
    assert "- read_file:" in captured["system"]
    assert "- write_file:" not in captured["system"]   # MUTATE hidden from tool list
    assert "- delete:" not in captured["system"]        # CONFIRM hidden from tool list
    assert "DISABLED" in captured["system"]


async def test_mutating_tool_runs_when_allowed():
    calls = []
    res = await run_agent("write", _tools(calls), chat_fn=_scripted([
        _act("write_file", path="x.yml", content="a"),
        _act("finish", summary="wrote"),
    ]), allowed_levels={READ, MUTATE})
    assert ("write_file", {"path": "x.yml", "content": "a"}) in calls


async def test_confirm_tool_needs_confirm_level():
    calls = []
    res = await run_agent("delete", _tools(calls), chat_fn=_scripted([
        _act("delete", path="x.yml"),
        _act("finish", summary="x"),
    ]), allowed_levels={READ, MUTATE})  # CONFIRM withheld
    assert calls == []
    assert "disabled" in res.steps[0]["observation"]["error"]


async def test_unknown_tool_reported():
    res = await run_agent("x", _tools([]), chat_fn=_scripted([
        _act("nope_tool"),
        _act("finish", summary="x"),
    ]), allowed_levels={READ})
    assert "unknown tool" in res.steps[0]["observation"]["error"]


async def test_loop_guard_stops_repeated_action():
    # Same (tool,args) repeated → loop guard stops before the step limit.
    # Threshold is two-strike (third call refused) — was 1-strike originally, but
    # strong models legitimately re-read a file after a fix and the aggressive
    # cut-off was killing real-world tasks.
    res = await run_agent("loop", _tools([]),
                          chat_fn=lambda s, m: _act("read_file", path="a"),
                          allowed_levels={READ}, max_steps=8)
    assert not res.finished
    assert "loop" in res.stopped_reason
    assert len(res.steps) == 2   # ran twice, refused the third identical attempt


async def test_step_limit_stops_runaway_with_varying_actions():
    # Distinct args each step → no loop-guard trip; must stop at the step limit.
    n = [0]
    def chat(s, m):
        n[0] += 1
        return _act("read_file", path=f"file{n[0]}.yml")
    res = await run_agent("loop", _tools([]), chat_fn=chat,
                          allowed_levels={READ}, max_steps=3)
    assert not res.finished
    assert "step limit" in res.stopped_reason
    assert len(res.steps) == 3


async def test_tool_exception_does_not_crash_loop():
    def boom(a): raise ValueError("kaboom")
    tools = {"bad": Tool("bad", READ, "boom", boom)}
    res = await run_agent("x", tools, chat_fn=_scripted([
        _act("bad"),
        _act("finish", summary="recovered"),
    ]), allowed_levels={READ})
    assert res.finished
    assert "kaboom" in res.steps[0]["observation"]["error"]


async def test_invalid_action_then_recovers():
    res = await run_agent("x", _tools([]), chat_fn=_scripted([
        "I will think about it (no action block)",
        _act("finish", summary="ok"),
    ]), allowed_levels={READ})
    assert res.finished


async def test_needed_levels_reported_when_blocked():
    # Agent tries a disabled MUTATE tool, then finishes → needed_levels names it.
    res = await run_agent("write", _tools([]), chat_fn=_scripted([
        _act("write_file", path="x.yml", content="a"),
        _act("finish", summary="cannot, disabled"),
    ]), allowed_levels={READ})
    assert res.needed_levels == [MUTATE]


async def test_needed_levels_empty_when_nothing_blocked():
    res = await run_agent("read", _tools([]), chat_fn=_scripted([
        _act("read_file", path="a.yml"),
        _act("finish", summary="done"),
    ]), allowed_levels={READ})
    assert res.needed_levels == []


def _tools_writeval(calls, issues_seq):
    """write_file returns scripted validation observations from issues_seq (a list,
    one per call); read_file is a no-op."""
    state = {"i": 0}
    def _write(a):
        calls.append(("write_file", a))
        obs = {"saved": a.get("path")}
        obs.update(issues_seq[min(state["i"], len(issues_seq) - 1)])
        state["i"] += 1
        return obs
    return {
        "write_file": Tool("write_file", MUTATE, "write", _write),
        "search_docs": Tool("search_docs", READ, "docs", lambda a: {"modules": []}),
    }


async def test_finish_gate_blocks_premature_finish_on_invalid_module():
    calls = []
    tools = _tools_writeval(calls, [
        {"invalid_modules": ["ansible.builtin.ufw_allow"], "issues": []},  # 1st write: bad
        {"invalid_modules": [], "issues": []},                              # 2nd write: fixed
    ])
    res = await run_agent("write hardening", tools, chat_fn=_scripted([
        _act("write_file", path="h.yml", content="bad"),
        _act("finish", summary="done"),       # premature → must be blocked
        _act("write_file", path="h.yml", content="fixed"),
        _act("finish", summary="fixed it"),
    ]), allowed_levels={READ, MUTATE})
    assert res.finished
    assert len(calls) == 2          # it was forced to write a corrected version
    assert res.summary == "fixed it"


async def test_finish_gate_only_nudges_once():
    # If the model insists on finishing with errors, we don't loop forever.
    calls = []
    tools = _tools_writeval(calls, [{"invalid_modules": ["ansible.builtin.nope"], "issues": []}])
    res = await run_agent("write", tools, chat_fn=_scripted([
        _act("write_file", path="h.yml", content="bad"),
        _act("finish", summary="try1"),   # blocked once
        _act("finish", summary="try2"),   # accepted (already nudged)
    ]), allowed_levels={READ, MUTATE})
    assert res.finished and res.summary == "try2"


async def test_finish_gate_passes_clean_write():
    calls = []
    tools = _tools_writeval(calls, [{"invalid_modules": [], "issues": []}])
    res = await run_agent("write", tools, chat_fn=_scripted([
        _act("write_file", path="ok.yml", content="good"),
        _act("finish", summary="clean"),
    ]), allowed_levels={READ, MUTATE})
    assert res.finished and res.summary == "clean"
    assert len(calls) == 1   # no forced rewrite


async def test_no_progress_guard_stops_repeated_failures():
    # A run tool that keeps failing with VARYING args (evades exact-dup guard) must
    # still be stopped by the no-progress guard after 3 failures.
    n = [0]
    def fail(a): return {"status": "failed", "rc": 2, "failures": [{"msg": "boom"}]}
    tools = {"run_playbook": Tool("run_playbook", CONFIRM, "run", fail)}
    def chat(s, m):
        n[0] += 1
        return _act("run_playbook", playbook=f"p{n[0]}.yml")  # different args each time
    res = await run_agent("run it", tools, chat_fn=chat,
                          allowed_levels={READ, CONFIRM}, max_steps=10)
    assert not res.finished
    assert "no progress" in res.stopped_reason
    assert len(res.steps) == 3


async def test_no_progress_resets_on_success():
    # fail, fail, SUCCESS, fail, fail → never 3 consecutive → no early stop.
    seq = [{"status": "failed"}, {"status": "failed"}, {"status": "successful"},
           {"status": "failed"}, {"status": "failed"}]
    st = {"i": 0}
    def run(a):
        o = seq[st["i"]]; st["i"] += 1; return o
    tools = {"preview": Tool("preview", MUTATE, "p", run)}
    replies = [_act("preview", playbook=f"p{i}.yml") for i in range(5)] + [_act("finish", summary="ok")]
    res = await run_agent("x", tools, chat_fn=_scripted(replies),
                          allowed_levels={READ, MUTATE}, max_steps=10)
    assert res.finished   # reset at the success prevented a 3-in-a-row stop


async def test_warnings_surfaced_from_write_validation():
    # A write whose validation returns a warning (e.g. lockout) must surface it on
    # the result, regardless of what the model says in finish.
    def w(a): return {"saved": a.get("path"),
                      "issues": [{"severity": "warning", "message": "risk of locking yourself out"}],
                      "invalid_modules": []}
    tools = {"write_file": Tool("write_file", MUTATE, "w", w)}
    res = await run_agent("harden", tools, chat_fn=_scripted([
        _act("write_file", path="harden.yml", content="x"),
        _act("finish", summary=""),   # model ignores the warning / empty summary
    ]), allowed_levels={READ, MUTATE})
    assert res.finished
    assert any("locking yourself out" in w for w in res.warnings)
