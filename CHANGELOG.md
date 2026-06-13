# Changelog

All notable changes to Playforge are documented here. The format loosely follows
[Keep a Changelog](https://keepachangelog.com/); versions are tagged in git.

## [0.0.3] — 2026-06-13

### Added
- **`make lab-regression`** — one-shot API regression for a dockerized VM lab:
  controller + targets preflight, runs the configured playbooks, and prints a
  compact JSON report (`scripts/lab_regression.sh`). Exits non-zero when
  preflight fails or any run is not `ok`/`successful`, so it slots straight
  into CI. Knobs via env vars (`PROJECT_ID`, `BASE_URL`, `INVENTORY_PATH`,
  `PLAYBOOKS_CSV`, `EXTRA_VARS_JSON`, `REQUEST_TIMEOUT_SEC`).
- **`POST /api/runs/preflight`** — side-effect-free check (no Run row written)
  that validates controller prerequisites (`ansible`, `ansible-playbook`,
  `sudo`, optional `sshpass`/`passlib`) and, in `check` mode, probes for
  `python3-apt` across the app interpreter, `/usr/bin/python3`, and `dpkg`.
  Optionally probes target reachability with an ad-hoc `ping`.
- **`GET /api/runs/{run_id}`** — global run detail by id (without the project
  prefix), with `stats`, `failures`, `artifacts`, `diagnostics`, and resolved
  `project_name`/`environment_name`. Returns 404 for unknown ids.
- **`diagnostics` in run responses** — `start_run`, `preview_run`,
  WebSocket `summary`, and `run_detail` now include a deterministic, dedup'd
  list of actionable hints for common Ansible failures. Rules:
  `missing_sudo`, `missing_python3_apt`, `ssh_restart_lockout`,
  `firewall_permission_denied`, `host_unreachable`, `ssh_auth_failed`,
  `dns_resolution_failed`, `disk_full`, `package_not_found`,
  `missing_collection`, `become_password_required`,
  `vault_password_required`, plus a generic `check_mode_failure` nudge.
  Hints are phrased for non-expert operators (cause first, then fix).
- **Docker healthcheck** — `GET /health` now does a `SELECT 1` against the
  app DB and returns 503 on failure. The base `docker-compose.yml` wires it
  to `healthcheck:`, so `docker ps` shows `(healthy)` once the app is up
  and orchestrators can restart it on a real outage.
- **WebSocket regression test** — `test_run_ws_summary_includes_diagnostics`
  exercises `/api/runs/ws` end-to-end via `starlette.testclient` and asserts
  the `diagnostics` field is on the `summary` event.

### Changed
- **Preflight: `sudo` and `python3-apt` are no longer required on the
  controller.** Both run on the *target*, not the controller; flagging them
  as hard requirements made a slim controller image (e.g. `python:3.12-slim`)
  fail preflight for perfectly fine remote-only playbooks. They are still
  reported in `checks` so the UI can suggest them when needed.

## [0.0.2] — 2026-06-07

### Security
- **WebSocket auth bypass (`/api/runs/ws`)** — HTTP middleware doesn't cover
  the WS scope, so when `ANSIBLE_GUI_PASSWORD` was set a LAN attacker could
  open a WebSocket, send a `RunIn`, and execute arbitrary playbooks with the
  configured credentials. Fixed by checking the session cookie in the handler
  and closing with code 4401 before `accept()` (with regression test).
- **Path-traversal hardening** — the file/move/mkdir/delete endpoints (and the
  agent) now reject any path inside the project's own `.git/` (a planted hook or
  `core.hooksPath`/`fsmonitor` in `.git/config` would run on the next auto-commit)
  and any path resolving to the project root itself (previously a 500).

### Added
- **Agent mode** — a tool-using AI agent that operates on a project through a
  fixed toolset (read files, search docs/project, lint, write/move/mkdir,
  install Galaxy deps, dry-run `--check`, real run, fetch module docs). ReAct
  loop with a JSON-action protocol; bounded by a step limit + loop-guard.
  Three opt-in trust levels (read-only → allow changes → allow delete/web);
  every mutation is its own git commit. Self-checks: after writing a playbook
  it sees rule violations and hallucinated modules and must fix them before
  finishing; can preview a run, read failures, and correct.
- **Shared assistant conversation** — slide-out dock and the full `/assistant`
  page share one conversation via a global Alpine store, live-synced and
  persisted to `localStorage` (7-day TTL). Full feature parity (agent in both).
- **All four credential kinds wired** — SSH key, vault password, and now
  become password (`--become-password-file`) and WireGuard keys (written to
  0600 files, exposed as the `wireguard_keys` extra-var).
- **Run-secrets protection** (per project, default off) — run-generated keys
  and passwords are kept out of git (added to `.gitignore`).
- **Free-form file tree** — create files & folders, rename, move/drag YAML
  between directories from the Files tab (sandboxed, each change committed).
- **Per-file git history** — `storage.file_history` + `storage.file_at(sha)`;
  History tab in the file editor with View + Restore-into-editor.
- **Credential test endpoint** (`POST /api/credentials/{id}/test`) — probe an
  SSH key or become password against a project's inventory; per-host ✓/✗ in
  the UI. Catches "key wrong / sudo expired" before a 30-task playbook does.
- **Ad-hoc command builder** — module/args/host-pattern form in the project's
  Run tab (the underlying endpoint already supported it).
- **`--limit` multiselect** — click groups/hosts from inventory to build the
  Ansible `:`-separated limit string instead of typing it.
- **Run-detail artifact preview** — expand any artifact inline (8 KB tail) or
  deep-link into the editor via `?file=<path>#files`.
- **Per-schedule timezone** — `Schedule.timezone` (IANA name, '' = UTC);
  `CronTrigger` and `next_fire_iso` are tz-aware; UI datalist of common zones.
- **Deterministic YAML autofix** — common model mistakes (unquoted
  `key: {{ var }}`) are repaired before validation/retry.
- **Type checking** — `mypy` baseline + a CI `types` job.

### Changed
- **`ai.py` (867 LOC) → `ai/` package** — split into `providers`, `explain`,
  `generate`, `suggest`, `narrate`, `chat`, `runbook`, `agent_runner`. Public
  API (`ai.explain_failure`, `ai.chat`, `ai.setting`, `ai._provider_*`, the
  `_*_BACKENDS` dicts) re-exported from `__init__.py`. Submodules look up
  monkeypatched symbols via `ai.<name>` at call time so tests propagate.
- **`project.html` (1981 LOC) → 1046 + 5 partials** in `templates/project/`.
- **`run_playbook` / `run_playbook_sync`** — duplicated event-capture extracted
  into a single `_EventCollector` (capture + finalize).
- **Agent loop-guard** softened: stop on the third identical `(tool, args)`
  instead of the second — strong models legitimately re-read after a fix.
- **`credentials.secret_path` → `credentials.has_secret`** — returns a bool;
  the path pointed at an encrypted file, easy to misuse with `ssh -i`.
- **`/api/runs`** — start_run / preview / WS validate that `project_id` exists
  on disk and return 404 instead of writing a "failed" Run row for a typo.
- **New project scaffold** no longer hard-codes `host_key_checking = False`
  in `ansible.cfg` — commented out so users opt in.
- **`_project_envvars`** respects `roles_path` / `collections_path` declared
  in the project's `ansible.cfg`; env vars beat config in Ansible and we were
  silently overriding the user's settings.
- Self-critique (`ai.validate_responses`) tri-state: `auto` (default; on only
  for strong cloud providers), `1` (always), `0` (off).
- BM25 doc index + `known_modules` + chat cache all flush together after a
  Galaxy install/remove (was only the BM25 index).

### Fixed
- Latent bug where online module-doc lookup passed a positional arg where a
  keyword was required (the web-docs path would have errored).
- Agent UX: only permitted tools are advertised; meta-questions ("what can
  you do?") answered directly; blocked tool returns the permission needed so
  the UI can offer "Enable & retry".
- Sync runner now drops `private_data_dir` even on failure so ephemeral
  vault/ssh key material doesn't linger.
- Rescued failures on overall-successful runs no longer surfaced as
  actionable failures (sync path now matches async).

### Tests / infra
- 356 tests (was 290+) — 18 new in `tests/test_review_fixes.py` covering
  every change above (incl. the WebSocket auth guard).
- mypy: 0 errors across 46 source files.

## [0.0.1] — 2026
- Initial release: import/run Ansible projects, git-per-project, run history,
  scheduler, environments, Vault, inventory editor, Galaxy, encrypted credentials,
  and the first AI features (chat, NL→playbook, remediation, preview, runbook,
  RAG/BM25, neuro-symbolic validation). GPL-3.0.
