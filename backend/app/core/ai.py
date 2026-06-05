"""AI helper: explain Ansible task failures via configurable LLM backends.

Three providers, picked at runtime from `app_settings` (UI-editable):
  - Anthropic Claude (`anthropic.Anthropic`)
  - OpenAI / OpenAI-compatible (raw httpx — works against any OpenAI-shaped API)
  - Ollama (raw httpx)

Settings (provider, keys, models, timeout) come from `core.settings_store.get(...)`.
The legacy env vars (`ANTHROPIC_API_KEY`, `OLLAMA_URL`, etc.) are still consulted
as fallbacks via the spec table in `settings_store` — so a fresh install with
docker-compose env vars set works without ever touching the UI.

Routes that wrap this stay sync `def` (network IO blocks; FastAPI threadpool).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re

import anthropic
import httpx
import yaml

from app.core import doc_index
from app.core import playbook_rules
from app.core.ai_validate import downgrade_confidence, validate_text
from app.core.playbook_builder import build_playbook, BuilderError
from app.core.settings_store import get as setting


# ---- Provider resolution ---------------------------------------------------

async def resolve_provider() -> tuple[str | None, dict]:
    """Return (provider_name, config) — config is a dict of the params that
    provider needs. Provider is None if nothing is configured."""
    chosen = (await setting("ai.provider")).strip().lower() or "auto"
    timeout = float(await setting("ai.timeout_seconds") or 120)

    anth_key = await setting("ai.anthropic_key")
    open_key = await setting("ai.openai_key")
    ollama_url = await setting("ai.ollama_url")

    if chosen == "anthropic" or (chosen == "auto" and anth_key):
        if not anth_key:
            return None, {}
        return "anthropic", {
            "api_key": anth_key,
            "model": await setting("ai.anthropic_model"),
            "timeout": timeout,
        }
    if chosen == "openai" or (chosen == "auto" and open_key):
        if not open_key:
            return None, {}
        return "openai", {
            "api_key": open_key,
            "model": await setting("ai.openai_model"),
            "base_url": (await setting("ai.openai_base_url")).rstrip("/"),
            "timeout": timeout,
        }
    if chosen == "ollama" or (chosen == "auto" and ollama_url):
        if not ollama_url:
            return None, {}
        return "ollama", {
            "url": ollama_url.rstrip("/"),
            "model": await setting("ai.ollama_model"),
            "timeout": timeout,
            "keep_alive": (await setting("ai.ollama_keep_alive")) or "30m",
        }
    return None, {}


def _ollama_chat(cfg: dict, messages: list, *, fmt: str | None = None,
                 temperature: float = 0.3, num_predict: int = 512) -> dict:
    """POST to Ollama's /api/chat with keep_alive so the model stays resident in RAM
    between calls (avoids a multi-second cold reload on every request). Returns the
    parsed JSON response."""
    payload: dict = {
        "model": cfg["model"], "stream": False,
        "keep_alive": cfg.get("keep_alive", "30m"),
        "messages": messages,
        "options": {"temperature": temperature, "num_predict": num_predict},
    }
    if fmt:
        payload["format"] = fmt
    with httpx.Client(timeout=cfg["timeout"]) as c:
        r = c.post(f"{cfg['url']}/api/chat", json=payload)
        r.raise_for_status()
        return r.json()


async def ai_enabled() -> bool:
    provider, _ = await resolve_provider()
    return provider is not None


# ---- Shared prompt ---------------------------------------------------------

EXPLAIN_SYSTEM = """You are an Ansible expert helping a DevOps engineer diagnose a failed task.
Given a failure record, write 2-4 short sentences explaining:
1. The most likely root cause
2. A concrete next step or fix

Be specific: reference the host, task name, module, and the key error fragment.
If the failure result reveals a misconfiguration (missing host key, wrong port, denied auth,
missing var, etc.), name it directly. If it's a network/SSH/DNS issue, say so.
Do not invent details that aren't in the record. Plain text — no markdown headers."""


CRITIQUE_SYSTEM = """You audit Ansible failure explanations for accuracy.
Given an ORIGINAL FAILURE RECORD and a PROPOSED EXPLANATION, list any claims in
the explanation that are NOT supported by the failure record. A claim is
"unsupported" if the failure record contains no evidence for it.

Be strict but specific. Examples of unsupported claims:
- saying "firewall is blocking" when the record only shows a DNS error
- naming a module/parameter that doesn't appear in the record
- assuming the host runs Debian when the OS is unknown

Reply ONLY as JSON: {"unsupported_claims": ["specific quoted claim 1", ...]}.
Empty list if every claim is grounded."""


def _format_user_prompt(failure: dict, playbook: str) -> str:
    result_json = json.dumps(failure.get("result") or {}, indent=2, default=str)[:3000]
    stderr_excerpt = (failure.get("stderr") or "")[:1500]
    return (
        f"Playbook: {playbook or '(unknown)'}\n"
        f"Host: {failure.get('host') or '(unknown)'}\n"
        f"Task: {failure.get('task') or '(unknown)'}\n\n"
        f"Result JSON:\n{result_json}\n\n"
        f"Stderr excerpt:\n{stderr_excerpt}"
    )


def _format_critique_prompt(failure: dict, explanation: str) -> str:
    return (
        "ORIGINAL FAILURE RECORD:\n"
        + json.dumps(failure, indent=2, default=str)[:3000]
        + "\n\nPROPOSED EXPLANATION:\n"
        + explanation
    )


# ---- Backends --------------------------------------------------------------

def _anthropic_explain(failure: dict, playbook: str, cfg: dict) -> dict:
    client = anthropic.Anthropic(api_key=cfg["api_key"], timeout=cfg["timeout"])
    resp = client.messages.create(
        model=cfg["model"], max_tokens=512,
        system=[{"type": "text", "text": EXPLAIN_SYSTEM, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": _format_user_prompt(failure, playbook)}],
    )
    text = next((b.text for b in resp.content if b.type == "text"), "")
    return {
        "provider": "anthropic", "model": resp.model, "explanation": text,
        "input_tokens": resp.usage.input_tokens, "output_tokens": resp.usage.output_tokens,
        "cache_read_input_tokens": getattr(resp.usage, "cache_read_input_tokens", 0),
    }


def _anthropic_critique(failure: dict, explanation: str, cfg: dict) -> dict:
    client = anthropic.Anthropic(api_key=cfg["api_key"], timeout=cfg["timeout"])
    resp = client.messages.create(
        model=cfg["model"], max_tokens=400, system=CRITIQUE_SYSTEM,
        messages=[{"role": "user", "content": _format_critique_prompt(failure, explanation)}],
        output_config={
            "format": {
                "type": "json_schema",
                "schema": {
                    "type": "object",
                    "properties": {"unsupported_claims": {"type": "array", "items": {"type": "string"}}},
                    "required": ["unsupported_claims"],
                    "additionalProperties": False,
                },
            }
        },
    )
    text = next((b.text for b in resp.content if b.type == "text"), "")
    return _parse_critique(text)


def _openai_chat(messages: list, cfg: dict, *, json_mode: bool = False, max_tokens: int = 512) -> dict:
    """Single call to an OpenAI-compatible /chat/completions endpoint."""
    body: dict = {
        "model": cfg["model"], "messages": messages,
        "max_tokens": max_tokens, "temperature": 0.3,
    }
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    with httpx.Client(timeout=cfg["timeout"]) as c:
        r = c.post(f"{cfg['base_url']}/chat/completions",
                   headers={"Authorization": f"Bearer {cfg['api_key']}"}, json=body)
        r.raise_for_status()
        return r.json()


def _openai_explain(failure: dict, playbook: str, cfg: dict) -> dict:
    data = _openai_chat(
        [{"role": "system", "content": EXPLAIN_SYSTEM},
         {"role": "user", "content": _format_user_prompt(failure, playbook)}],
        cfg,
    )
    msg = data["choices"][0]["message"]["content"]
    usage = data.get("usage", {})
    return {
        "provider": "openai", "model": data.get("model", cfg["model"]),
        "explanation": msg.strip(),
        "input_tokens": usage.get("prompt_tokens", 0),
        "output_tokens": usage.get("completion_tokens", 0),
    }


def _openai_critique(failure: dict, explanation: str, cfg: dict) -> dict:
    data = _openai_chat(
        [{"role": "system", "content": CRITIQUE_SYSTEM},
         {"role": "user", "content": _format_critique_prompt(failure, explanation)}],
        cfg, json_mode=True, max_tokens=400,
    )
    return _parse_critique(data["choices"][0]["message"]["content"])


def _ollama_explain(failure: dict, playbook: str, cfg: dict) -> dict:
    data = _ollama_chat(cfg, [
        {"role": "system", "content": EXPLAIN_SYSTEM},
        {"role": "user", "content": _format_user_prompt(failure, playbook)},
    ], temperature=0.3, num_predict=512)
    return {
        "provider": "ollama", "model": data.get("model", cfg["model"]),
        "explanation": (data.get("message") or {}).get("content", "").strip(),
        "input_tokens": data.get("prompt_eval_count") or 0,
        "output_tokens": data.get("eval_count") or 0,
    }


def _ollama_critique(failure: dict, explanation: str, cfg: dict) -> dict:
    data = _ollama_chat(cfg, [
        {"role": "system", "content": CRITIQUE_SYSTEM},
        {"role": "user", "content": _format_critique_prompt(failure, explanation)},
    ], fmt="json", temperature=0.1, num_predict=400)
    return _parse_critique((data.get("message") or {}).get("content", ""))


def _parse_critique(raw: str) -> dict:
    s = (raw or "").strip()
    if s.startswith("```"):
        s = s.strip("`")
        if s.startswith("json"):
            s = s[4:].lstrip()
    try:
        parsed = json.loads(s)
    except json.JSONDecodeError:
        return {"unsupported_claims": [], "_parse_error": True, "_raw_excerpt": raw[:200]}
    if not isinstance(parsed, dict):
        return {"unsupported_claims": [], "_parse_error": True}
    claims = parsed.get("unsupported_claims") or []
    return {"unsupported_claims": [str(c) for c in claims if c]}


# ---- Playbook generation from natural language -----------------------------

GENERATE_SYSTEM = """You turn a plain-language request into ONE Ansible play.
Reply with ONLY a JSON object (no markdown, no prose) of this exact shape:
{
  "name": "<short play name>",
  "hosts": "<host pattern or group>",
  "become": <true|false>,
  "gather_facts": <true|false>,
  "tasks": [
    {
      "name": "<task name>",
      "module": "<fully.qualified.module.name>",
      "args": { <module arguments as a JSON object> },
      "tags": ["<tag>"],
      "when": "<jinja condition or empty>"
    }
  ]
}
Rules:
- Use ONLY real, fully-qualified modules you are certain exist (ansible.builtin.*,
  and well-known community collections). Never invent module names or parameters.
- Prefer idempotent modules (package/apt/dnf, service/systemd, copy, template,
  lineinfile, user, git, file, get_url, ...).
- For values the user must supply, use Jinja vars like "{{ app_repo }}".
- Keep it minimal and correct. Omit "tags"/"when" if not needed."""


def _gen_user_prompt(description: str, hosts: str) -> str:
    return (f"Request:\n{description}\n\n"
            f"Target hosts: {hosts or 'all'}\n\n"
            "Return the JSON play now.")


def _parse_spec(raw: str) -> dict:
    s = (raw or "").strip()
    if s.startswith("```"):
        s = s.strip("`")
        if s.startswith("json"):
            s = s[4:].lstrip()
    try:
        parsed = json.loads(s)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"AI did not return valid JSON: {e}")
    if not isinstance(parsed, dict):
        raise RuntimeError("AI returned JSON that is not an object")
    return parsed


def _to_builder_spec(parsed: dict, hosts: str) -> dict:
    """Convert the model's JSON play into the shape `build_playbook` expects
    (tasks carry `args_yaml` strings rather than nested objects)."""
    tasks_out = []
    for t in parsed.get("tasks") or []:
        if not isinstance(t, dict):
            continue
        args = t.get("args") or {}
        args_yaml = ""
        if isinstance(args, dict) and args:
            args_yaml = yaml.dump(args, default_flow_style=False, sort_keys=False).strip()
        tags = t.get("tags") or []
        if isinstance(tags, str):
            tags = [s.strip() for s in tags.split(",") if s.strip()]
        tasks_out.append({
            "name": (t.get("name") or "").strip(),
            "module": (t.get("module") or "").strip(),
            "args_yaml": args_yaml,
            "tags": tags,
            "when": (t.get("when") or "").strip(),
        })
    gf = parsed.get("gather_facts")
    return {
        "name": (parsed.get("name") or "Generated play").strip(),
        "hosts": (parsed.get("hosts") or hosts or "all").strip(),
        "become": bool(parsed.get("become")),
        "gather_facts": False if gf is False else None,
        "tasks": tasks_out,
    }


def _anthropic_generate(description: str, hosts: str, cfg: dict) -> dict:
    client = anthropic.Anthropic(api_key=cfg["api_key"], timeout=cfg["timeout"])
    resp = client.messages.create(
        model=cfg["model"], max_tokens=1500, system=GENERATE_SYSTEM,
        messages=[{"role": "user", "content": _gen_user_prompt(description, hosts)}],
    )
    return _parse_spec(next((b.text for b in resp.content if b.type == "text"), ""))


def _openai_generate(description: str, hosts: str, cfg: dict) -> dict:
    data = _openai_chat(
        [{"role": "system", "content": GENERATE_SYSTEM},
         {"role": "user", "content": _gen_user_prompt(description, hosts)}],
        cfg, json_mode=True, max_tokens=1500,
    )
    return _parse_spec(data["choices"][0]["message"]["content"])


def _ollama_generate(description: str, hosts: str, cfg: dict) -> dict:
    data = _ollama_chat(cfg, [
        {"role": "system", "content": GENERATE_SYSTEM},
        {"role": "user", "content": _gen_user_prompt(description, hosts)},
    ], fmt="json", temperature=0.2, num_predict=1500)
    return _parse_spec((data.get("message") or {}).get("content", ""))


_GENERATE_BACKENDS = {
    "anthropic": _anthropic_generate,
    "openai":    _openai_generate,
    "ollama":    _ollama_generate,
}


async def generate_playbook(description: str, *, hosts: str = "") -> dict:
    """Generate a playbook spec from a natural-language description, then run it
    through the same deterministic anti-hallucination check used for failure
    explanations — so AI-authored playbooks are flagged when they cite modules
    that don't exist."""
    if not (description or "").strip():
        raise RuntimeError("description is required")
    provider, cfg = await resolve_provider()
    if provider is None:
        raise RuntimeError("no AI provider configured")
    parsed = await asyncio.to_thread(_GENERATE_BACKENDS[provider], description, hosts, cfg)
    spec = _to_builder_spec(parsed, hosts)
    yaml_text = build_playbook(spec)  # raises BuilderError on a malformed spec
    validation = validate_text(yaml_text)
    return {"provider": provider, "model": cfg.get("model", ""),
            "spec": spec, "yaml": yaml_text, "validation": validation}


# ---- AI remediation (suggest a fix for a failed task) ----------------------

def _provider_json(provider: str, system: str, user: str, cfg: dict, *, max_tokens: int = 2000) -> dict:
    """Run a JSON-returning completion against whichever provider is active."""
    if provider == "anthropic":
        client = anthropic.Anthropic(api_key=cfg["api_key"], timeout=cfg["timeout"])
        resp = client.messages.create(model=cfg["model"], max_tokens=max_tokens, system=system,
                                      messages=[{"role": "user", "content": user}])
        return _parse_spec(next((b.text for b in resp.content if b.type == "text"), ""))
    if provider == "openai":
        data = _openai_chat([{"role": "system", "content": system},
                             {"role": "user", "content": user}], cfg, json_mode=True, max_tokens=max_tokens)
        return _parse_spec(data["choices"][0]["message"]["content"])
    if provider == "ollama":
        data = _ollama_chat(cfg, [{"role": "system", "content": system}, {"role": "user", "content": user}],
                            fmt="json", temperature=0.2, num_predict=max_tokens)
        return _parse_spec((data.get("message") or {}).get("content", ""))
    raise RuntimeError(f"unknown provider: {provider}")


SUGGEST_SYSTEM = """You are an Ansible expert fixing a failed task. Given the failure
record and the playbook that produced it, propose ONE concrete fix.
Reply with ONLY a JSON object:
{
  "root_cause": "<one sentence>",
  "fix_summary": "<what to change and why, 1-3 sentences>",
  "target_path": "<relative path of the file to edit, usually the playbook>",
  "new_content": "<COMPLETE new content of target_path with the fix applied, or null>",
  "manual_steps": ["<step>"]
}
Rules:
- If a file edit fixes it, return the WHOLE corrected file in new_content (not a diff).
- Only propose editing the playbook you were given (or a vars file it references).
- Use real modules/parameters only; keep the change minimal.
- If the failure is environmental (auth, DNS, unreachable host, missing key), set
  new_content to null and put the human actions in manual_steps."""


def _suggest_user_prompt(failure: dict, playbook_path: str, playbook_content: str) -> str:
    return (
        "FAILURE RECORD:\n"
        + json.dumps(failure, indent=2, default=str)[:3000]
        + f"\n\nPLAYBOOK ({playbook_path or 'unknown'}):\n"
        + (playbook_content or "(not available)")[:6000]
    )


async def suggest_fix(failure: dict, *, playbook_path: str = "", playbook_content: str = "") -> dict:
    """Propose a concrete remediation for a failed task. The proposed file content
    is run through the anti-hallucination check, and is only ever *returned* — the
    caller applies it as an explicit, reviewable write."""
    provider, cfg = await resolve_provider()
    if provider is None:
        raise RuntimeError("no AI provider configured")
    user = _suggest_user_prompt(failure, playbook_path, playbook_content)
    parsed = await asyncio.to_thread(_provider_json, provider, SUGGEST_SYSTEM, user, cfg)

    new_content = parsed.get("new_content")
    if not isinstance(new_content, str) or not new_content.strip():
        new_content = None
    return {
        "provider": provider, "model": cfg.get("model", ""),
        "root_cause": str(parsed.get("root_cause") or ""),
        "fix_summary": str(parsed.get("fix_summary") or ""),
        "target_path": str(parsed.get("target_path") or playbook_path or ""),
        "new_content": new_content,
        "manual_steps": [str(s) for s in (parsed.get("manual_steps") or []) if s],
        "validation": validate_text(new_content or ""),
    }


# ---- Pre-run impact narration ----------------------------------------------

def _provider_text(provider: str, system: str, user: str, cfg: dict, *, max_tokens: int = 600) -> str:
    """Run a plain-text completion against whichever provider is active."""
    if provider == "anthropic":
        client = anthropic.Anthropic(api_key=cfg["api_key"], timeout=cfg["timeout"])
        resp = client.messages.create(model=cfg["model"], max_tokens=max_tokens, system=system,
                                      messages=[{"role": "user", "content": user}])
        return next((b.text for b in resp.content if b.type == "text"), "")
    if provider == "openai":
        data = _openai_chat([{"role": "system", "content": system},
                             {"role": "user", "content": user}], cfg, max_tokens=max_tokens)
        return data["choices"][0]["message"]["content"]
    if provider == "ollama":
        data = _ollama_chat(cfg, [{"role": "system", "content": system}, {"role": "user", "content": user}],
                            temperature=0.3, num_predict=max_tokens)
        return (data.get("message") or {}).get("content", "")
    raise RuntimeError(f"unknown provider: {provider}")


NARRATE_SYSTEM = """You explain, in plain language for a DevOps engineer, what an Ansible
run WILL DO before it runs. You are given the tasks that a --check (dry-run) reported as
"would change", grouped by host. Write 2-5 short sentences:
- summarise what changes and on which hosts,
- call out anything risky (service restarts, package removals, file overwrites, reboots),
- if the list is empty, say the hosts already match desired state (no drift).
Be concrete and brief. Plain text, no markdown headers."""


def _narrate_user_prompt(changes: list[dict], playbook: str) -> str:
    if not changes:
        return f"Playbook: {playbook or '(unknown)'}\nCheck-mode reported NO changes."
    lines = [f"- {c.get('host', '?')}: {c.get('task', '?')}" for c in changes[:80]]
    return (f"Playbook: {playbook or '(unknown)'}\n"
            f"Would-change tasks ({len(changes)}):\n" + "\n".join(lines))


async def narrate_plan(changes: list[dict], *, playbook: str = "") -> dict:
    """Plain-language narration of a check-mode preview ('what this run will do')."""
    provider, cfg = await resolve_provider()
    if provider is None:
        raise RuntimeError("no AI provider configured")
    text = await asyncio.to_thread(_provider_text, provider, NARRATE_SYSTEM,
                                   _narrate_user_prompt(changes, playbook), cfg)
    return {"provider": provider, "model": cfg.get("model", ""), "narration": (text or "").strip()}


# ---- Conversational assistant ----------------------------------------------

# A fenced block whose first line declares a path: ``` lang\n# file: path/to/x ...```
_FILE_BLOCK_RE = re.compile(
    r"```[ \t]*([a-zA-Z0-9_+-]*)[ \t]*\n"          # opening fence + optional lang
    r"[ \t]*#[ \t]*file:[ \t]*([^\n`]+?)[ \t]*\n"  # first line: `# file: <path>`
    r"(.*?)```",                                    # body up to closing fence
    re.DOTALL,
)

# Reject anything that would escape the project (absolute paths, .., leading ~/).
_UNSAFE_PATH = re.compile(r"(^/|^~|(^|/)\.\.(/|$))")


def extract_files(reply: str) -> list[dict]:
    """Pull saveable files out of an assistant reply.

    Each ```block``` whose first line is `# file: <path>` becomes
    {"path","content","lang"}. Paths that try to escape the project are dropped.
    Returns [] when the reply has no file blocks (plain chat answer)."""
    out: list[dict] = []
    seen: set[str] = set()
    for lang, raw_path, body in _FILE_BLOCK_RE.findall(reply or ""):
        stripped = raw_path.strip()
        # Check safety on the RAW path first (so /etc/passwd, ~/x, ../e are rejected
        # before any normalisation could mask them).
        if not stripped or _UNSAFE_PATH.search(stripped):
            continue
        path = stripped.lstrip("./")
        if not path or _UNSAFE_PATH.search(path) or path in seen:
            continue
        seen.add(path)
        out.append({"path": path, "content": body, "lang": (lang or "").strip()})
    return out


CHAT_SYSTEM = """You are an expert Ansible/DevOps assistant embedded in Playforge, a self-hosted Ansible UI.
Help the user do what they describe: explain modules, write or fix playbooks / inventory /
vars, debug failures, suggest best practices.

Rules — follow strictly:
- Answer ONLY what was asked. No filler, no restating the question, no apologies.
- Use ONLY real, fully-qualified modules you are certain exist (ansible.builtin.*, known
  community.* collections). NEVER invent module names, parameters, or facts.
- If you are not sure, say "I'm not sure" and state what you'd check — do not guess.
- Prefer idempotent modules. Keep it minimal.
- If the request is ambiguous, ask ONE short clarifying question instead of guessing.

OUTPUT FILES — important:
- Put each file you produce in its OWN fenced code block whose FIRST line is a path
  comment, so the GUI can save it to the right place:
    ```yaml
    # file: playbooks/site.yml
    - hosts: all
      ...
    ```
- A task that uses a Jinja template (`ansible.builtin.template`) or runs a script
  MUST be accompanied by that template/script as its own file block, e.g.:
    ```jinja
    # file: templates/nginx.conf.j2
    server { listen {{ http_port }}; }
    ```
    ```bash
    # file: scripts/setup.sh
    #!/usr/bin/env bash
    set -euo pipefail
    ```
- Use the conventional layout: playbooks/ , templates/ (for .j2), files/ , scripts/ .
- The block body must be the file's exact content (valid for its type), no prose inside."""


def _provider_chat(provider: str, system: str, messages: list[dict], cfg: dict, *, max_tokens: int = 1200) -> str:
    """Multi-turn text completion. `messages` are [{role: user|assistant, content}]."""
    if provider == "anthropic":
        client = anthropic.Anthropic(api_key=cfg["api_key"], timeout=cfg["timeout"])
        resp = client.messages.create(model=cfg["model"], max_tokens=max_tokens,
                                      system=system, messages=messages)
        return next((b.text for b in resp.content if b.type == "text"), "")
    if provider == "openai":
        data = _openai_chat([{"role": "system", "content": system}, *messages], cfg, max_tokens=max_tokens)
        return data["choices"][0]["message"]["content"]
    if provider == "ollama":
        data = _ollama_chat(cfg, [{"role": "system", "content": system}, *messages],
                            temperature=0.3, num_predict=max_tokens)
        return (data.get("message") or {}).get("content", "")
    raise RuntimeError(f"unknown provider: {provider}")


# Tiny in-memory cache: identical (provider, model, history) → identical reply.
# Resets on restart; bounded so it can't grow without limit. Helps most with a slow
# local model and repeated/regenerated questions.
_CHAT_CACHE: dict[str, dict] = {}
_CHAT_CACHE_MAX = 128


def _chat_cache_key(provider: str, model: str, messages: list[dict]) -> str:
    blob = json.dumps([provider, model, messages], sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


async def chat(messages: list[dict], *, project_context: str = "", project_root=None,
               project_id: str | None = None) -> dict:
    """Conversational assistant. The reply is checked for hallucinated modules AND
    for structural playbook mistakes; identical requests are served from cache.
    `project_context` (the current project's structure) grounds answers — a first
    retrieval step so the model references the user's real files. `project_root`,
    if given, lets the rule check expand any `roles:` the reply references."""
    provider, cfg = await resolve_provider()
    if provider is None:
        raise RuntimeError("no AI provider configured")
    clean = [{"role": m.get("role"), "content": str(m.get("content", ""))}
             for m in (messages or [])
             if m.get("role") in ("user", "assistant") and str(m.get("content", "")).strip()][-20:]
    if not clean or clean[-1]["role"] != "user":
        raise RuntimeError("the last message must be from the user")

    system = CHAT_SYSTEM
    if project_context.strip():
        system += ("\n\nCURRENT PROJECT (reference these real names; don't invent files):\n"
                   + project_context.strip())

    # RAG: retrieve the most relevant real Ansible modules (BM25 over ansible-doc)
    # and ground the answer in them — cuts module hallucination at the source. For the
    # top hits we pull the FULL parameter signature from ansible-doc so the model uses
    # real option names instead of inventing them (the "bredzi przy argumentach" fix).
    retrieved = await asyncio.to_thread(doc_index.search_modules, clean[-1]["content"], 6)
    if retrieved:
        lines = []
        for i, d in enumerate(retrieved):
            sig = await asyncio.to_thread(doc_index.format_module_signature, d["module"]) if i < 4 else None
            lines.append("- " + (sig or f"{d['module']}: {d['description']}"))
        system += ("\n\nRELEVANT ANSIBLE MODULES (retrieved from this host's ansible-doc — use these "
                   "real, fully-qualified names and ONLY these parameter names; `*` marks required; "
                   "do not invent modules or options):\n" + "\n".join(lines))

    # Phase C (optional, off by default): if the user named a fully-qualified module
    # we don't have installed, look it up on docs.ansible.com so the model still gets
    # its real parameters instead of inventing them. Only fires when ai.web_docs is on.
    if (await setting("ai.web_docs")) == "1":
        import re as _re
        local = doc_index._module_corpus()
        local_names = {n for n, _ in local}
        mentioned = {m for m in _re.findall(r"(?:[a-z][a-z0-9_]*\.){2}[a-z][a-z0-9_]*", clean[-1]["content"])
                     if m not in local_names}
        web_lines = []
        for mod in list(mentioned)[:3]:
            sig = await asyncio.to_thread(doc_index.format_module_signature, mod, 18, True)
            if sig:
                web_lines.append("- " + sig)
        if web_lines:
            system += ("\n\nADDITIONAL MODULES (fetched from docs.ansible.com — real, but not installed "
                       "in this image; the user must install the collection to run them):\n"
                       + "\n".join(web_lines))

    # Full project RAG: retrieve the most relevant file *contents* (BM25 over the
    # project's playbooks/vars/roles/templates) so the assistant can answer about the
    # user's real repo ("what does this playbook do?") instead of guessing.
    if project_id:
        from app.core import project_index
        hits = await asyncio.to_thread(project_index.search, project_id, clean[-1]["content"], 4)
        if hits:
            blocks = [f"### {h['path']}\n{h['snippet']}" for h in hits]
            system += ("\n\nRELEVANT FILES FROM THIS PROJECT (the user's real content — base answers on "
                       "these; quote paths when referring to them):\n" + "\n\n".join(blocks))

    model = cfg.get("model", "")
    key = _chat_cache_key(provider, model, clean + [{"role": "system", "content": system}])
    if key in _CHAT_CACHE:
        return {**_CHAT_CACHE[key], "cached": True}

    reply = (await asyncio.to_thread(_provider_chat, provider, system, clean, cfg) or "").strip()
    issues = playbook_rules.check_reply(reply, project_root)

    # Auto-retry once if a small model produced invalid YAML (the battery showed
    # ~20% of replies had this). We feed the broken reply + the exact errors back and
    # ask only for a corrected version. Cheap insurance, only on a real parse failure.
    yaml_errs = [i for i in issues if "invalid YAML" in i.get("message", "")]
    if yaml_errs:
        fix_msgs = clean + [
            {"role": "assistant", "content": reply},
            {"role": "user", "content":
                "The YAML you produced doesn't parse: "
                + "; ".join(e["message"] for e in yaml_errs)
                + ". Re-output the SAME files corrected, valid YAML only, same `# file:` blocks."},
        ]
        retry = (await asyncio.to_thread(_provider_chat, provider, system, fix_msgs, cfg) or "").strip()
        retry_issues = playbook_rules.check_reply(retry, project_root)
        # Keep the retry only if it actually fixed the YAML (no new parse errors).
        if retry and not [i for i in retry_issues if "invalid YAML" in i.get("message", "")]:
            reply, issues = retry, retry_issues

    validation = validate_text(reply)
    validation["playbook_issues"] = issues
    result = {"provider": provider, "model": model, "reply": reply, "validation": validation,
              "retrieved_modules": [d["module"] for d in retrieved],
              "files": extract_files(reply)}

    _CHAT_CACHE[key] = result
    if len(_CHAT_CACHE) > _CHAT_CACHE_MAX:
        _CHAT_CACHE.pop(next(iter(_CHAT_CACHE)))
    return result


# ---- Auto-runbook (living project documentation) ---------------------------

RUNBOOK_SYSTEM = """You write a concise operational runbook (Markdown) for an Ansible project,
aimed at a DevOps engineer who must understand and operate it. Based ONLY on the playbooks,
inventories and roles you are given (never invent), produce:

# <Project> — Runbook
## Overview
One or two sentences: what this project manages.
## Playbooks
For each playbook: what it does, its target hosts, key tasks, and important tags/vars.
## Inventory & environments
## How to run
Short, practical notes / example invocations.
## Cautions
Anything risky: service restarts, destructive tasks (removals), reboots, SSH lockout risks.

Keep it tight and accurate. Output Markdown only — no preamble."""


async def generate_runbook(context: str, *, project_name: str = "") -> dict:
    """Generate a Markdown runbook from a summary of the project's files."""
    provider, cfg = await resolve_provider()
    if provider is None:
        raise RuntimeError("no AI provider configured")
    user = f"Project: {project_name or '(unnamed)'}\n\n{context}"
    md = await asyncio.to_thread(_provider_text, provider, RUNBOOK_SYSTEM, user, cfg, max_tokens=1800)
    return {"provider": provider, "model": cfg.get("model", ""), "markdown": (md or "").strip()}


# ---- Public dispatch -------------------------------------------------------

_EXPLAIN_BACKENDS = {
    "anthropic": _anthropic_explain,
    "openai":    _openai_explain,
    "ollama":    _ollama_explain,
}
_CRITIQUE_BACKENDS = {
    "anthropic": _anthropic_critique,
    "openai":    _openai_critique,
    "ollama":    _ollama_critique,
}


async def explain_failure(failure: dict, *, playbook: str = "", validate: bool = True) -> dict:
    """Caller is async (FastAPI route). Backends are sync (SDK + httpx in sync mode),
    so we hop them onto the threadpool to keep the event loop free."""
    provider, cfg = await resolve_provider()
    if provider is None:
        raise RuntimeError("no AI provider configured")
    backend = _EXPLAIN_BACKENDS[provider]
    result = await asyncio.to_thread(backend, failure, playbook, cfg)

    if not (result.get("explanation") or "").strip():
        result["explanation"] = ("The model returned an empty explanation. Try again, or set a "
                                  "stronger provider (Anthropic / OpenAI) in Settings → AI helper.")

    # Layer 1: deterministic check (fast, no network).
    validation = validate_text(result.get("explanation", ""))

    # Layer 2: LLM self-critique. Global flag in settings can disable it.
    validate_globally = (await setting("ai.validate_responses")) != "0"
    if validate and validate_globally:
        try:
            critique = await asyncio.to_thread(
                _CRITIQUE_BACKENDS[provider], failure, result["explanation"], cfg
            )
            validation["self_critique"] = critique
            downgrade_confidence(validation, unsupported_claims=len(critique.get("unsupported_claims", [])))
        except Exception as e:
            validation["self_critique_error"] = str(e)

    result["validation"] = validation
    return result


# ---- Model probing (for the Settings page) ---------------------------------

def probe_anthropic(api_key: str, *, timeout: float = 30.0) -> list[dict]:
    client = anthropic.Anthropic(api_key=api_key, timeout=timeout)
    return [{"id": m.id, "display_name": getattr(m, "display_name", m.id)}
            for m in client.models.list()]


def probe_openai(api_key: str, base_url: str, *, timeout: float = 30.0) -> list[dict]:
    base = base_url.rstrip("/")
    with httpx.Client(timeout=timeout) as c:
        r = c.get(f"{base}/models", headers={"Authorization": f"Bearer {api_key}"})
        r.raise_for_status()
        data = r.json()
    models = data.get("data") or []
    return [{"id": m["id"], "display_name": m["id"]} for m in models if "id" in m]


def probe_ollama(url: str, *, timeout: float = 30.0) -> list[dict]:
    base = url.rstrip("/")
    with httpx.Client(timeout=timeout) as c:
        r = c.get(f"{base}/api/tags")
        r.raise_for_status()
        data = r.json()
    return [{"id": m["name"], "display_name": m["name"]} for m in (data.get("models") or [])]
