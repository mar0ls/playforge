"""Agentic loop: the assistant can act on a project through a fixed set of tools.

Design choices, deliberately conservative:

- **JSON action protocol, not native function-calling.** Local models (deepseek,
  devstral) don't do tool-calls reliably, so each turn the model emits one
  ```action {json}``` block; we parse, run the tool, feed back an observation, and
  loop (a ReAct loop). This works on weak models and is fully auditable.
- **Tools are injected**, not imported here — keeps this module pure and testable
  (the API layer wires in real storage/galaxy/web functions).
- **Three trust levels** per tool: read-only (always), mutating (needs agent mode
  on), and confirm (destructive/external — caller must pre-approve). The loop
  refuses a tool whose level isn't permitted and tells the model why.
- **Bounded**: at most `max_steps` actions, so a confused model can't loop forever.

The model is expected to finish with a ```action {"tool": "finish", "summary": …}```.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Callable

READ = "read"        # always allowed
MUTATE = "mutate"    # only when agent mode is enabled
CONFIRM = "confirm"  # destructive/external — must be in the allowed set explicitly


@dataclass
class Tool:
    name: str
    level: str
    description: str
    run: Callable[[dict], dict]   # args -> observation dict (JSON-serialisable)


@dataclass
class AgentResult:
    steps: list[dict] = field(default_factory=list)   # [{tool, args, observation}]
    summary: str = ""
    finished: bool = False
    stopped_reason: str = ""
    needed_levels: list[str] = field(default_factory=list)  # disabled levels the agent tried to use
    warnings: list[str] = field(default_factory=list)  # safety warnings (e.g. lockout) from validation


_ACTION_RE = None  # compiled lazily


def _extract_action(text: str) -> dict | None:
    """Pull the JSON object from a ```action {…}``` block (or a bare {…} fallback)."""
    import re
    global _ACTION_RE
    if _ACTION_RE is None:
        _ACTION_RE = re.compile(r"```(?:action)?\s*\n?(\{.*?\})\s*```", re.DOTALL)
    m = _ACTION_RE.search(text or "")
    blob = m.group(1) if m else None
    if blob is None:
        # last resort: first {...} spanning the string
        s = (text or "").strip()
        if s.startswith("{") and s.endswith("}"):
            blob = s
    if not blob:
        return None
    try:
        obj = json.loads(blob)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        return None


AGENT_SYSTEM = """You are a DevOps assistant operating on ONE Ansible project through tools.

Work in a loop. Each step, respond with EXACTLY ONE action as a fenced block:
```action
{"tool": "<name>", "args": { ... }}
```
You will receive an OBSERVATION, then continue. When you are done — whether you
changed anything or are just answering — end with:
```action
{"tool": "finish", "args": {"summary": "<your answer or what you did>"}}
```

Rules:
- ONE action per message, nothing else outside the fenced block.
- If the user just ASKS A QUESTION (e.g. "what can you do?", "explain X", or wants
  a playbook shown but you cannot write it), DO NOT use tools — answer directly in a
  single `finish` with the full answer (include any playbook in the summary).
- Only use a tool when it actually moves the task forward. Never repeat an action
  that already failed — change approach or finish.
- Inspect before you change: read files / search docs first.
- After writing a YAML file, CHECK the observation: if it reports `issues` (errors)
  or `invalid_modules`, those are real problems in YOUR output — fix them (write the
  corrected file) before finishing. Use `search_docs` to find the right module.
- If `preview` is available, dry-run a playbook you wrote (it makes no changes); if it
  reports failures, fix them and preview again. Only use `run_playbook` when the user
  explicitly wants real changes applied.
- Use only the tools listed below. If a capability you need is not listed, it is
  disabled — say so in `finish` and tell the user which permission to enable.
- Use only real, fully-qualified modules. Don't invent files or paths.

Tools available to you right now:
{tools}
{disabled}"""


async def run_agent(
    goal: str,
    tools: dict[str, Tool],
    *,
    chat_fn: Callable[[str, list[dict]], str],
    allowed_levels: set[str],
    max_steps: int = 8,
) -> AgentResult:
    """Drive the ReAct loop. `chat_fn(system, messages)` returns the model's text
    (sync; caller wraps in a thread). `allowed_levels` gates which tools may run.

    Only tools the caller is allowed to use are advertised to the model — listing a
    tool it can't run just makes weak models waste steps failing on it. Disabled
    capabilities are summarised so the model can tell the user what to enable."""
    usable = {n: t for n, t in tools.items() if t.level in allowed_levels}
    tool_list = "\n".join(f"- {t.name}: {t.description}" for t in usable.values())
    disabled_levels = {t.level for t in tools.values() if t.level not in allowed_levels}
    disabled = ""
    if disabled_levels:
        hint = []
        if MUTATE in disabled_levels:
            hint.append("creating/editing files & installing collections (enable “allow changes”)")
        if CONFIRM in disabled_levels:
            hint.append("deleting files & fetching web docs (enable “allow delete / web”)")
        disabled = "\nDISABLED right now (you cannot do these): " + "; ".join(hint) + "."
    system = AGENT_SYSTEM.replace("{tools}", tool_list).replace("{disabled}", disabled)

    messages: list[dict] = [{"role": "user", "content": goal}]
    result = AgentResult()
    recent: list[str] = []   # signatures of recent actions, for loop detection
    needed: set[str] = set()  # disabled levels the model reached for
    open_errors: list[str] = []   # unresolved errors from the most recent write
    nudged = False                # only push back on a premature finish once
    no_progress = 0               # consecutive failing steps without a successful change
    warnings: list[str] = []      # safety warnings surfaced by validation (lockout, etc.)

    for _ in range(max_steps):
        reply = (chat_fn(system, messages) or "").strip()
        action = _extract_action(reply)
        if action is None:
            messages.append({"role": "assistant", "content": reply})
            messages.append({"role": "user", "content":
                             "No valid action block found. Respond with exactly one "
                             "```action {\"tool\":...,\"args\":...}``` block, or finish."})
            continue

        tool_name = str(action.get("tool", ""))
        raw_args = action.get("args")
        args: dict = raw_args if isinstance(raw_args, dict) else {}

        if tool_name == "finish":
            # Self-checking gate: if the last file the agent wrote still has hard
            # errors (hallucinated modules / error-level rule violations), push back
            # once and make it fix them before accepting the finish.
            if open_errors and not nudged:
                nudged = True
                messages.append({"role": "assistant", "content": reply})
                messages.append({"role": "user", "content":
                    "Don't finish yet — the file you wrote still has errors: "
                    + "; ".join(open_errors)
                    + ". Fix them (write the corrected file with real, fully-qualified "
                      "modules), then finish."})
                continue
            result.summary = str(args.get("summary", "")).strip()
            result.finished = True
            result.needed_levels = sorted(needed)
            return result

        # Stop on third identical (tool, args). Two-strike was too aggressive
        # — strong models legitimately re-read after a fix. The no-progress
        # guard below catches doomed loops whose args drift slightly.
        sig = tool_name + json.dumps(args, sort_keys=True, default=str)
        if recent.count(sig) >= 2:
            result.stopped_reason = (f"repeated the same action ('{tool_name}') without "
                                     "progress — stopped to avoid a loop")
            result.needed_levels = sorted(needed)
            return result
        recent.append(sig)

        tool = usable.get(tool_name)
        obs: dict
        if tool is None:
            # Either unknown or disabled — tell the model precisely, so it can
            # finish with advice instead of retrying.
            if tool_name in tools:
                needed.add(tools[tool_name].level)
                obs = {"error": f"tool '{tool_name}' is disabled right now. "
                                "Finish and tell the user which permission to enable."}
            else:
                obs = {"error": f"unknown tool '{tool_name}'. Usable: {', '.join(usable) or '(none)'}"}
        else:
            try:
                obs = tool.run(args)
            except Exception as e:  # a tool failing must not crash the loop
                obs = {"error": f"{type(e).__name__}: {e}"}

        # Track unresolved hard errors from the latest write, for the finish gate.
        # A fresh write (or move) replaces the previous verdict.
        if tool_name in ("write_file", "move") and "error" not in obs:
            errs = [f"invalid module {m}" for m in obs.get("invalid_modules", [])]
            errs += [i["message"] for i in obs.get("issues", []) if i.get("severity") == "error"]
            open_errors = errs
            # Collect warning-severity findings (e.g. SSH lockout) so the UI surfaces
            # the self-checking layer even if the model ignores them in its summary.
            for i in obs.get("issues", []):
                if i.get("severity") == "warning" and i["message"] not in warnings:
                    warnings.append(i["message"])
            result.warnings = warnings

        # No-progress guard: a failing step (tool error, or a run/preview that
        # returned "failed") counts as no progress; a clean result resets it. This
        # catches a model re-running a doomed playbook with slightly-varied args
        # (which the exact-dup guard above misses). Make it fix things, not spin.
        failed_step = ("error" in obs) or (obs.get("status") == "failed")
        no_progress = no_progress + 1 if failed_step else 0
        if no_progress >= 3:
            result.stopped_reason = ("3 steps in a row made no progress (repeated "
                                     "failures) — stopped. Fix the playbook, then retry.")
            result.needed_levels = sorted(needed)
            result.steps.append({"tool": tool_name, "args": args, "observation": obs})
            return result

        result.steps.append({"tool": tool_name, "args": args, "observation": obs})
        messages.append({"role": "assistant", "content": reply})
        messages.append({"role": "user", "content": "OBSERVATION:\n" + json.dumps(obs, default=str)[:4000]})

    result.stopped_reason = f"reached step limit ({max_steps})"
    result.needed_levels = sorted(needed)
    return result
