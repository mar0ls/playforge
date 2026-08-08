# Changelog

All notable changes to Playforge are documented here. The format loosely follows
[Keep a Changelog](https://keepachangelog.com/); versions are tagged in git.

## [0.9.0] — 2026-08-08

Published to Docker Hub. `docker compose up -d` now pulls an image instead of
building one.

### Changed
- **`docker-compose.yml` pulls `mar0ls/playforge`** instead of building from
  source. It runs standalone — one file, no clone. Building still works through
  the dev overlay, which is what `make build` now uses.
- **The setup token is logged on one line.** Under `docker compose logs` every
  line is prefixed with the service name, so the previous multi-line banner left
  the operator picking a token out of decorated output. `grep "SETUP TOKEN"`
  returns exactly one line; README and the setup page say so.

### Added
- **`DOCKERHUB.md`** — the Docker Hub page: tags, a copy-pasteable compose that
  needs no clone, first sign-in, upgrading, and the environment table. The
  release workflow syncs it, best-effort — a token without permission to edit the
  description must not fail a release.

### Notes
Rehearsed with `v0.9.0-rc1` before tagging, which found two release bugs the real
tag would have hit: the smoke test compared `/health` against the full tag so
every prerelease failed against itself, and the GitHub Release wasn't flagged as
a prerelease, so a candidate showed as the repository's current release.

Verified against the published image, not a local build: pulled from Docker Hub
on a machine with no sources, arm64 selected from the multi-arch manifest,
container reports healthy, `/data` created on the host, setup token issued,
first-admin flow completed, data surviving a restart with setup then closed. The
compose embedded in `DOCKERHUB.md` was extracted from the Markdown and run
verbatim.



Release preparation. The documentation now describes the product as it is, and
the API contract is stated rather than implied.

### Added
- **API stability statement** — `/api` paths and response fields won't change or
  disappear within a major version from 1.0 onward; new optional fields and
  endpoints can appear at any time, so parse tolerantly.
- **`CONTRIBUTING.md`** — how to get a working checkout, what a change needs, and
  the four things that bite people who don't know them yet: migrations are
  append-only, new routes need a capability, the WebSocket authenticates itself,
  and secrets never leave the API.
- **Issue templates** for bugs and features, with a contact link routing security
  reports to a private advisory instead of a public issue.

### Changed
- **README describes 0.8, not 0.0.7.** It still said "single-user local, no login"
  as the only access model. It now documents the three modes, the roles, both
  routes to a first administrator, login throttling and run isolation.

### Notes
No `/api/v1` prefix, deliberately. The API is consumed by this app's own frontend
and by scripts people write against their own instance — it isn't published as a
separate product, so a version segment would be ceremony without an audience. A
genuinely breaking change would arrive as `/api/v2` alongside the existing paths,
with `/api` kept working for at least one minor release after the CHANGELOG
announces it.

Publishing to Docker Hub needs one repository secret, `DOCKERHUB_TOKEN` — the
username lives in the workflow's `env`, since it's already public in the image
name and making it a secret would protect nothing. The release workflow refuses
to publish when the tag,
`__version__` and CHANGELOG disagree, and smoke-tests `/health` on the pushed
image before creating the GitHub release. A prerelease tag (`v0.9.0-rc1`)
publishes only its own tag and does not move `latest`, which is the cheap way to
exercise the pipeline first.

## [0.8.1] — 2026-08-08

Tests for the layers that were thinnest. Three bugs found writing them.

### Fixed
- **A schedule could be registered twice, and would then run the playbook twice.**
  `sync_schedule` relied on `add_job(replace_existing=True)`, which only dedupes
  against the jobstore — and the jobstore doesn't exist until the scheduler
  starts. Before that, jobs queue in `_pending_jobs` and a second add appends a
  duplicate instead of replacing it. The lifespan happens to start the scheduler
  before anything syncs, so this was latent; reordering those two lines in a
  refactor would have made every schedule fire twice. `sync_schedule` now removes
  before adding, so it is idempotent regardless of scheduler state.
- **`PUT /api/schedules/{id}` accepted a project or template that doesn't exist**,
  while `POST` refused them. A working schedule could be repointed at nothing,
  and the only symptom would be it quietly not running.
- **Security headers were missing from denied responses.** Starlette wraps
  middleware in reverse registration order, so `require_login` was outside
  `security_headers` and short-circuited 401/403 responses never reached it —
  which made `SECURITY.md`'s "on every response" false. Registration order
  swapped.

### Changed
- **Deleting a run template used by a schedule is now refused (409)** and names
  the schedules. A schedule holds a plain `template_id`, not a foreign key, so
  the delete used to leave it pointing at nothing; it then failed at fire time,
  which nobody is watching.

### Added
- `tests/test_auth_middleware.py` — the auth and authorisation middleware
  end-to-end through `TestClient`: all three modes, role enforcement per route,
  session revocation on disable/delete/role-change, cookie tampering and
  user-id substitution, security headers.
- `tests/test_schedules_api.py`, `tests/test_templates_api.py` — full CRUD,
  including cross-project access through the path and APScheduler staying in step
  with the database.

### Notes
Coverage: `main.py` 50% → 77%, `api/schedules.py` 43% → 100%,
`api/templates.py` 54% → 100%, total 75% → 78%. Suite is 737 tests, up from 664.

The authorisation policy was already unit-tested against the route table, but
that only proved the table was right — not that the middleware consults it. That
gap is what these tests close.

## [0.8.0] — 2026-08-07

Multi-user is enforced, not just modelled. Sign in as a named account, and the
role decides what you can call.

### Added
- **Accounts, roles and enforcement.** Sessions carry the user id inside the
  signed cookie; `admin` / `operator` / `viewer` map to capabilities, and every
  API route declares which one it needs. An unmapped route denies everyone, and a
  test enumerates the app's real route table so an endpoint can't ship without
  that decision being made.
- **Two ways to create the first admin.** `ANSIBLE_GUI_ADMIN_USER` /
  `ANSIBLE_GUI_ADMIN_PASSWORD` for automated deploys (both accept a `_FILE` form,
  so the password can come from a docker/k8s secret rather than `.env`), or a
  `/setup` page guarded by a one-time token printed to the container log.
- **Users page** at `/users` — create, change role, enable/disable, reset a
  password, delete. Linked in the sidebar for admins only.
- `runs.user_id` is now populated, over both HTTP and the WebSocket.

### Fixed
- **The session signing key could fall back to a constant.** If the credential
  master key couldn't be read, the key was derived from a fixed string in the
  source — survivable only because the shared password was also in the mix. With
  accounts there may be no shared password, so that fallback would have meant
  anyone could forge a session. It now fails loudly instead.
- **The run WebSocket checked the session but not the role.** HTTP middleware
  doesn't cover WS scope, so a viewer could have started a run through the socket
  that `POST /api/runs` refuses them. It now checks both.

### Notes
The setup page is token-gated for a specific reason: `docker-compose.yml`
publishes the port on 0.0.0.0, so between `docker compose up -d` and the operator
reaching a browser, an open setup page would let whoever got there first claim
the instance. Verified against a running container — setup without the token, and
with a wrong token, both return 403 and create no account.

Existing installs are unaffected. With no accounts the app stays in
single-password or open mode exactly as before, and a first boot in
single-password mode does not open setup or nag toward accounts.

Accounts are re-read from the database on every request rather than trusted from
the cookie, so disabling an account ends its sessions immediately. Verified live:
the same cookie goes from 200 to 401 across the disable.

Role enforcement verified over HTTP against the built image, one account per
role — reads open to all three, credential writes, user management, run-history
deletion and provider config admin-only, running a playbook operator-and-above,
and the resulting run attributed to the account that started it.

## [0.7.0] — 2026-08-05

User accounts and roles — the schema and store half. Nothing enforces roles yet;
that lands next, on top of this.

### Added
- **`users` table** — username (case-insensitive, unique), password hash, role,
  disabled flag, timestamps. Existing installs are untouched: with no accounts
  the app stays in single-password (or no-auth) mode exactly as before.
- **`runs.user_id`** — who started a run. Null for runs from before this, for
  scheduled runs, and while in single-password mode; the audit trail only means
  something once accounts exist. Closes the "Audyt" item from the TODO.
- **Password hashing with `hashlib.scrypt`** — stdlib, so no new dependency and
  no lockfile churn, and memory-hard rather than a bare hash. The cost parameters
  are stored inside each hash, so they can be raised later without invalidating
  anyone's password; `needs_rehash` flags stale ones and `authenticate` upgrades
  them transparently on the next successful login.
- **Roles: admin / operator / viewer**, expressed as capabilities (`read`, `run`,
  `write`, `secrets`, `admin`) so a route can ask for a capability instead of
  hardcoding a role list.
- **Last-admin protection** — the last active admin can't be demoted, disabled or
  deleted. An install with no admin can't be administered back to working.

### Notes
scrypt parameters were measured, not guessed. On the reference host (Docker
29.4.0, arm64): 2^14 = 34ms/16MB, 2^16 = 105ms/64MB, 2^17 = 209ms/128MB. N = 2^16
is the choice: OWASP's headline figure is 2^17, but 128MB per login attempt is
felt on the small self-hosted boxes this app runs on. Also worth knowing —
OpenSSL's default `maxmem` rejects any N above 2^14 outright, so it's passed
explicitly; without it `scrypt(n=2**16, …)` just raises "memory limit exceeded".

`authenticate` runs the KDF even when the account doesn't exist, so a wrong
username and a wrong password cost the same. Otherwise response time enumerates
accounts.

**Upgrade verified on a real database**, not just in unit tests: a v1 database
written by the 0.6.1 container, holding 7 runs and a project, came up as schema
v2 with the `users` table created, `user_id` added, every row intact, and a
second start applying nothing.

## [0.6.1] — 2026-08-05

Run isolation verified on a real host. 0.6.0 shipped it unverified — the Docker
daemon was down — so this replaces the guesses with measurements.

### It works, but the container needs two flags

Isolation is inert unless the app container runs with:

```yaml
security_opt:
  - seccomp=unconfined
  - systempaths=unconfined
```

Measured on Docker 29.4.0 (arm64, Docker Desktop), running as uid 1000:

| container settings | result |
|---|---|
| defaults | `bwrap: No permissions to create new namespace` |
| as root, default seccomp | same — it's the seccomp profile, not the uid |
| `seccomp=unconfined` | gets further, then `Can't mount proc: Operation not permitted` |
| `seccomp=unconfined` + `CAP_SYS_ADMIN` | still can't mount proc |
| `seccomp=unconfined` + `systempaths=unconfined` | **works** |
| `--privileged` | works, but defeats the purpose |

The default seccomp profile blocks the user-namespace clone, and Docker's masked
`/proc` paths block the mount ansible-runner's sandbox always performs. Both
`--security-opt` flags are needed; extra capabilities are not, and `--privileged`
is not.

### Verified end to end

Same playbook, same data volume, same image — only the isolation setting differs:

- **off** → run fails with `DATA VISIBLE - run can read /data/app.db`
- **on** → run passes, `/data/app.db` is not reachable

**Fails closed.** With isolation on and the flags missing, the run fails (rc 1, no
hosts touched) instead of quietly running unsandboxed.

### Changed
- `docker-compose.yml` carries the two `security_opt` lines, commented, next to
  the setting that needs them.
- `SECURITY.md` and `.env.example` state the flag requirement and the measured
  trade-off rather than describing isolation as if enabling the setting were
  enough.

### Notes
The flags weaken the app container's own confinement (no syscall filter,
unmasked `/proc`) to sandbox the runs inside it. Those are different threat
models and it's a real trade, not a free win: it protects your credential vault
and other projects from a bad playbook, at the cost of a thinner barrier between
the container and the host kernel. Left off by default for that reason.

## [0.6.0] — 2026-08-05

Run isolation, off by default. First half of the multi-user work: roles mean
little while every run can do anything on the controller.

### Added
- **Optional run isolation** (`run.isolation`, `ANSIBLE_GUI_RUN_ISOLATION`).
  Until now nothing was passed to ansible-runner to constrain a run, so a
  playbook — including one the AI generated — executed as the app user with the
  whole filesystem in reach: `/data/master.key`, `app.db`, every other project's
  repo. Enabled, runs go through ansible-runner's sandbox, which binds only
  `/bin` `/etc` `/usr` `/opt` read-only plus the project's own directory, so
  `/data` is invisible by simply not being bound.
- Wired into all three run paths: the streamed run, ad-hoc commands, and the
  agent's preview/run tools. The agent path resolves the setting in the async
  route, because its tool callbacks are sync and run on a worker thread.
- `bubblewrap` added to the image.

### Notes
Off by default deliberately. The sandbox needs the host to allow unprivileged
user namespaces; a run that won't start is worse than one that isn't sandboxed.
Turn it on and confirm a run still works before relying on it.

`docker`/`podman` are accepted as alternative mechanisms, giving a container per
run, but they need the engine's socket inside the app container — root-equivalent
access to the host. That buys isolation and loses more elsewhere, so `bwrap` is
the default when isolation is on.

The unit tests cover the settings gate and the exact kwargs handed to
ansible-runner, stopping at that boundary — whether bwrap works on a given kernel
isn't something a unit test can assert. See 0.6.1 for the measured behaviour.

## [0.5.0] — 2026-08-05

Closing the two open holes in the HTTP surface, and writing down the access model.

### Added
- **Failed-login throttling** — `POST /login` had no limit, which made the single
  shared password a guessing oracle for anyone who could reach the port. 5 failed
  attempts per client address now trigger a lockout that doubles from 30s to a
  15min cap, and it fails closed: the correct password is refused while locked.
  Counted on `request.client.host`, not `X-Forwarded-For` — uvicorn runs without
  `--proxy-headers`, so the header would be attacker-controlled. Behind a reverse
  proxy the lockout becomes global rather than per-client, which fails closed too.
- **Security headers on every response** — CSP, `X-Frame-Options: DENY`,
  `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`.
- **`SECURITY.md`** — how to report a vulnerability, and the access model stated
  plainly: single-tenant, UI access equals shell access on the controller.

### Notes
`script-src` keeps `'unsafe-inline'` and `'unsafe-eval'`. All ten page templates
carry an inline `<script>` block and the vendored htmx builds handlers with
`Function()`/`eval()` (verified, not assumed), so dropping them blanks the UI. The
enforceable value is elsewhere: `connect-src 'self'` leaves injected script
nowhere to exfiltrate to, `frame-ancestors 'none'` stops clickjacking, and
`script-src 'self'` still bars code from another origin — which is worth having
now that no asset comes from a CDN.

Verified over real HTTP against a running app: headers present, lockout trips on
exactly the 5th attempt, correct password refused while locked, all seven
authenticated pages still render.

## [0.4.0] — 2026-08-01

Tests for the HTTP and scheduler edges. Two bugs found doing it.

### Fixed
- **Schedules with no timezone fired in the container's local zone, not UTC.**
  `''` means UTC per the API, the model comment and the docstring, but
  `CronTrigger.from_crontab()` with no timezone falls back to the *process* local
  zone. With `TZ` unset (the default image) that happens to be UTC, so it looked
  fine; set `TZ` on the container — a normal thing to do — and every schedule that
  didn't name a timezone shifted. Worse, `next_fire_iso` computes blank-timezone
  times in real UTC, so the "next fire" in the UI disagreed with when the job
  actually ran. UTC is now passed explicitly.
- **`file_at` dropped the trailing newline of every historical version.**
  GitPython strips it from command output, so "View / Restore past version"
  handed back content that differed from what was committed. Reads now use
  `strip_newline_in_stdout=False`.

### Added
- `tests/test_scheduler.py` — `_fire` was entirely untested: the path that turns a
  cron tick into a Run row, including a failing runner (which must still close the
  Run row, or the UI shows "running" forever), a disabled schedule, and a template
  deleted out from under a schedule. Plus cron/timezone validation, job sync, and
  DST behaviour around the Europe/Warsaw spring-forward.
- `tests/test_credentials_api.py` — secret lifecycle: encrypted at rest, never
  present in an API response, kept on a rename, rotated on update, and the file
  removed when the row is deleted.
- `tests/test_projects_api_paths.py` — path containment across read/write/delete/
  move/mkdir, file lifecycle, history and restore, and import-path validation.

### Notes
Coverage: `scheduler.py` 41% → 98%, `api/credentials.py` 35% → 63%,
`api/projects.py` 34% → 41%, total 71% → 74%. Suite is 527 tests, up from 446.

`~/…` is not a traversal in this codebase — pathlib doesn't expand `~` when
joining and `_resolve_safe` deliberately never calls `expanduser()`, so it becomes
a literal directory inside the project. Pinned by a test so nobody "fixes" it.

## [0.3.0] — 2026-08-01

Measurement for the AI layer, and provider calls that survive a rate limit.

### Added
- **Golden set for the self-checking layers** (`backend/tests/golden/`, harness in
  `tests/test_golden_rules.py`). Ten cases pinning the rule engine and the module
  validator, as data files rather than code, so adding a regression case is one
  file. The AI tests all mock the provider, which meant nothing measured whether
  the *checking* still worked when a prompt or model changed — this closes that.
  Deterministic and offline: a failure here is ours, not the model's.
  The harness also fails when a rule has no case, so the set can't rot silently.
- **Stable `rule` ids on findings.** `playbook_rules` findings carried only a
  severity and a prose message, so any assertion on them broke when the wording
  changed. Findings now also carry `rule` (e.g. `ufw-lockout-no-ssh`), which is
  what the golden set matches on. Additive — existing consumers read `message`
  and `severity` and are unaffected.
- **Retry with backoff for transient provider failures.** A 429 from a hosted
  provider, or a 503 while Ollama loads a model, used to surface as a hard error;
  there was no retry anywhere in the AI layer. Now 408/409/425/429/5xx and
  transport errors get up to 3 attempts with exponential backoff and jitter, and
  `Retry-After` is honoured (capped at 30s). 4xx config and auth errors are never
  retried — a wrong API key should fail immediately, not look like a hang.
  Streams retry only until the first token, since after that the text is already
  on screen and a retry would duplicate it. The Anthropic SDK does its own
  retrying, so its budget is set explicitly rather than stacked with ours.

### Notes
Coverage of `app/core/ai/providers.py` went from 34% to 58%; the suite is 446
tests, up from 407.

## [0.2.0] — 2026-07-31

Reproducible builds; air-gap and ad-hoc gaps closed.

### Added
- **`backend/requirements.lock`** — 73 packages pinned with hashes, generated by
  `pip-compile` in a linux container so resolution matches the image. The Dockerfile
  installs with `--require-hashes`. Runtime deps were floor-pinned (`>=`) with no
  lockfile, so two builds of the same tag could ship different versions — the image
  was running ansible-core 2.21.2 against a `>=2.17` pin. Regenerate: `make lock`.
- **Ad-hoc commands take credentials.** `POST /api/runs/adhoc` accepts
  `credential_ids`; `run_adhoc` injects the SSH key, SSH password and become
  password exactly as `run_playbook` does. Open since 0.0.1: ad-hoc against hosts
  needing key or sudo auth failed with "Permission denied" while a normal run of
  the same project worked. Credential resolution now lives in one
  `_resolve_credentials()` used by both paths. The UI reuses whatever is selected
  in the Run form.
- **`agent_tools.normalize_escapes()`** — weaker models (deepseek-coder-v2) emit
  file bodies as one line with literal `\n`, so the written file is a single line
  and Ansible won't parse it. Converts only when the content has `\n` escapes and
  no real newline, so a playbook legitimately containing `\n` in a string is left
  alone. Explicit replacement rather than `unicode_escape`, which corrupts
  non-ASCII. Reported to the agent as `normalized_escapes`.

### Changed
- **Monaco is vendored into the image** (`/opt/playforge/vendor`, fetched at build
  time against a pinned SHA-256) and served from `/vendor`. It was the last
  runtime CDN dependency, which made the README's "fully offline" claim false.
  Deliberately outside `/app/app` — the dev compose override bind-mounts that
  directory and would hide it.
- README and `THIRD_PARTY_LICENSES.md` now describe the actual air-gap and build
  reproducibility position.

### Notes
`TODO.md`: the `run_detail` 500 item was already fixed — all three `json.loads`
calls have had `or "[]"` guards for some time. Struck rather than re-fixed.

## [0.1.0] — 2026-07-30

Release plumbing. No product changes.

### Added
- **Versioned schema migrations** (`app/models/migrations.py`). The applied
  version is stored in `schema_meta` and reported by `/health`. Steps are ordered
  and idempotent; a database from any 0.0.x is detected as unstamped and brought
  forward on first start. Replaces `_soft_migrate`, which could only add nullable
  columns and didn't record which schema a database was on. An older build started
  against a newer database logs a warning instead of failing obscurely later.
  Verified against real 0.0.1 and 0.0.7 databases.
- **`scripts/backup.sh` / `scripts/restore.sh`** (+ `make backup` / `make restore`).
  Snapshots the DB through SQLite's online backup API — a plain `cp` under WAL can
  miss committed transactions — plus `master.key` and the project repos, into one
  0600 tarball with a manifest. Restore won't overwrite a populated data dir
  without `--force`, and moves the old one aside instead of deleting it.
- **`app.__version__`** as the single source of truth, surfaced in `/health`, the
  OpenAPI schema and the sidebar footer.
- **`.github/workflows/release.yml`** — builds and pushes multi-arch
  (amd64 + arm64) on a `v*` tag, refuses to publish when the tag, `__version__`
  and `CHANGELOG.md` disagree, and smoke-tests `/health` on the pushed image.
  Unused until 0.9, when image publishing starts.

### Fixed
- Sidebar showed a hardcoded `v0.1`, wrong since 0.0.2.
- `backups/` added to `.gitignore` — the default backup destination is inside the
  repo and the archives contain `master.key`.

### Upgrading from 0.0.x
Back up (`scripts/backup.sh`), then rebuild. `./data` migrates in place on first
start; `/health` should report `"schema_version": 1`.

## [0.0.7] — 2026-06-30

### Added
- **Streaming chat** — responses streamed token-by-token via `POST /api/ai/chat/stream`
  (newline-delimited JSON); the dock and full page populate live, with a fallback to the
  non-streaming endpoint. Most beneficial for long generations and slow local models.
- **One-click "Fix with agent"** — on a failed run, the button sends the agent to read
  the run (`get_run`), fix the playbook, and preview it. Stays human-reviewable: watch it
  in the dock, then re-run to confirm.
- **Clear run history** — `DELETE /api/runs` and a dashboard button; resets the stats and
  the run list. (Project files and git artifacts are kept.)

A demo of the streaming assistant has been added to the README.

### Fixed
- Streaming chat no longer shows a duplicate "thinking…" bubble; the in-flight reply
  bubble shows it until the first token arrives.

## [0.0.6] — 2026-06-28

### Added
- **Logo & branding** — Playforge logo as the favicon and sidebar mark, plus a
  README hero image. Heavy design sources stay out of the repo; only optimized
  derivatives are committed.
- **Code coverage in CI** — pytest-cov report uploaded to Codecov on every run,
  with a coverage badge in the README.

## [0.0.5] — 2026-06-20

### Added
- **SSH password credential** — new `ssh_password` credential kind for password
  (non-key) SSH logins. Encrypted in the Credentials vault like the rest, selected
  on the Run tab, injected at run time via sshpass — no secret in the inventory.
  Credential Test supports it. Closes the gap where password logins had to be put
  inline in `hosts`.

### Changed
- **Docker image & container renamed `ansible-gui` → `playforge`** (compose,
  Makefile, CI, git identity). Env vars keep the `ANSIBLE_GUI_*` prefix for
  backward compatibility. After upgrading, `docker compose up` creates the new
  `playforge` container — remove the old one with `docker rm -f ansible-gui`.

## [0.0.4] — 2026-06-17

### Fixed
- **First run crashed on a fresh host** — Docker creates the bind-mounted `./data`
  owned by root, but the app runs as uid 1000, so the first `docker compose up`
  died with `PermissionError: '/data/projects'`. A new entrypoint starts as root,
  fixes `/data` ownership once, then drops to the `app` user via `gosu` (uvicorn
  still runs non-root). Existing root-owned data dirs are repaired automatically
  on the next start.

### Added
- **`.env.example`** — copy to `.env` for password / master key / AI provider
  config. `.env` is now git-ignored.

### Changed
- **README quick start** — corrected clone URL (`mar0ls/playforge`) and documented
  the optional `.env` step and host-side `./data` state.

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
