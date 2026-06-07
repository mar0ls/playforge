"""Multi-turn assistant: RAG over modules + project files, YAML retry, reply cache."""
from __future__ import annotations

import asyncio
import functools
import hashlib
import json
import re

from app.core import doc_index
from app.core import playbook_rules


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
    """Pull `# file: path` fenced blocks out of a reply."""
    out: list[dict] = []
    seen: set[str] = set()
    for lang, raw_path, body in _FILE_BLOCK_RE.findall(reply or ""):
        stripped = raw_path.strip()
        # Check the raw path first — otherwise ./../etc/passwd would slip through.
        if not stripped or _UNSAFE_PATH.search(stripped):
            continue
        # Strip a single leading "./" only — lstrip("./") would eat real dotfiles
        # (`.env` → `env`) and mangle paths like `.../foo`.
        path = stripped[2:] if stripped.startswith("./") else stripped
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


# In-process reply cache. Bounded; resets on restart.
_CHAT_CACHE: dict[str, dict] = {}
_CHAT_CACHE_MAX = 128


def _chat_cache_key(provider: str, model: str, messages: list[dict]) -> str:
    blob = json.dumps([provider, model, messages], sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


def clear_chat_cache() -> None:
    # Cache keys hash a RAG-augmented system prompt — galaxy add/remove changes
    # that prompt, so stale entries would quote uninstalled/missing collections.
    _CHAT_CACHE.clear()


async def chat(messages: list[dict], *, project_context: str = "", project_root=None,
               project_id: str | None = None) -> dict:
    from app.core import ai

    provider, cfg = await ai.resolve_provider()
    if provider is None:
        raise RuntimeError("no AI provider configured")
    clean: list[dict[str, str]] = [
        {"role": str(m.get("role")), "content": str(m.get("content", ""))}
        for m in (messages or [])
        if m.get("role") in ("user", "assistant") and str(m.get("content", "")).strip()][-20:]
    if not clean or clean[-1]["role"] != "user":
        raise RuntimeError("the last message must be from the user")

    system = CHAT_SYSTEM
    if project_context.strip():
        system += ("\n\nCURRENT PROJECT (reference these real names; don't invent files):\n"
                   + project_context.strip())

    # RAG: BM25 over ansible-doc + full param signature for top hits, so the
    # reply is grounded in real module names + real option names.
    retrieved = await asyncio.to_thread(doc_index.search_modules, clean[-1]["content"], 6)
    if retrieved:
        lines = []
        for i, d in enumerate(retrieved):
            sig = await asyncio.to_thread(doc_index.format_module_signature, d["module"]) if i < 4 else None
            lines.append("- " + (sig or f"{d['module']}: {d['description']}"))
        system += ("\n\nRELEVANT ANSIBLE MODULES (retrieved from this host's ansible-doc — use these "
                   "real, fully-qualified names and ONLY these parameter names; `*` marks required; "
                   "do not invent modules or options):\n" + "\n".join(lines))

    # Optional web fallback for FQCNs we don't have installed (ai.web_docs).
    if (await ai.setting("ai.web_docs")) == "1":
        local = doc_index._module_corpus()
        local_names = {n for n, _ in local}
        mentioned = {m for m in re.findall(r"(?:[a-z][a-z0-9_]*\.){2}[a-z][a-z0-9_]*", clean[-1]["content"])
                     if m not in local_names}
        web_lines = []
        for mod in list(mentioned)[:3]:
            sig = await asyncio.to_thread(
                functools.partial(doc_index.format_module_signature, mod, 18, allow_web=True))
            if sig:
                web_lines.append("- " + sig)
        if web_lines:
            system += ("\n\nADDITIONAL MODULES (fetched from docs.ansible.com — real, but not installed "
                       "in this image; the user must install the collection to run them):\n"
                       + "\n".join(web_lines))

    # Project-file RAG: BM25 over the user's playbooks/vars/roles.
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

    reply = (await asyncio.to_thread(ai._provider_chat, provider, system, clean, cfg) or "").strip()

    # Quote `key: {{ var }}` etc. deterministically before retrying with the model.
    reply = playbook_rules.autofix_reply(reply)
    issues = playbook_rules.check_reply(reply, project_root)

    # One retry, only if a YAML block still won't parse.
    yaml_errs = [i for i in issues if "invalid YAML" in i.get("message", "")]
    if yaml_errs:
        fix_msgs = clean + [
            {"role": "assistant", "content": reply},
            {"role": "user", "content":
                "The YAML you produced doesn't parse: "
                + "; ".join(e["message"] for e in yaml_errs)
                + ". Re-output the SAME files corrected, valid YAML only, same `# file:` blocks."},
        ]
        retry = (await asyncio.to_thread(ai._provider_chat, provider, system, fix_msgs, cfg) or "").strip()
        retry_issues = playbook_rules.check_reply(retry, project_root)
        if retry and not [i for i in retry_issues if "invalid YAML" in i.get("message", "")]:
            reply, issues = retry, retry_issues

    validation = ai.validate_text(reply)
    validation["playbook_issues"] = issues
    result = {"provider": provider, "model": model, "reply": reply, "validation": validation,
              "retrieved_modules": [d["module"] for d in retrieved],
              "files": extract_files(reply)}

    _CHAT_CACHE[key] = result
    if len(_CHAT_CACHE) > _CHAT_CACHE_MAX:
        _CHAT_CACHE.pop(next(iter(_CHAT_CACHE)))
    return result
