"""NL → JSON play → `playbook_builder.build_playbook` → validate."""
from __future__ import annotations

import asyncio
import json

import anthropic
import yaml

from app.core.playbook_builder import BuilderError, build_playbook
from .providers import _openai_chat, _ollama_chat
# `validate_text` is reached via `ai.validate_text` (late binding) so tests
# monkeypatching it at the package level propagate here.


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
    # Tasks carry args_yaml (string) here, not nested objects — that's what build_playbook takes.
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
    from app.core import ai

    if not (description or "").strip():
        raise RuntimeError("description is required")
    provider, cfg = await ai.resolve_provider()
    if provider is None:
        raise RuntimeError("no AI provider configured")
    parsed = await asyncio.to_thread(_GENERATE_BACKENDS[provider], description, hosts, cfg)
    spec = _to_builder_spec(parsed, hosts)
    yaml_text = build_playbook(spec)  # BuilderError on malformed spec
    validation = ai.validate_text(yaml_text)
    return {"provider": provider, "model": cfg.get("model", ""),
            "spec": spec, "yaml": yaml_text, "validation": validation}
