# Contributing

## Getting a working checkout

```bash
git clone https://github.com/mar0ls/playforge.git
cd playforge
make build          # builds the image
make test           # runs the suite inside it
```

Run the suite **in the image**, not only in a local venv. The image pins
dependencies from `backend/requirements.lock`; a local venv drifts, and the two
have already disagreed in ways that mattered — a FastAPI version difference once
made a test pass while asserting nothing. `make test` is what CI runs.

For hot reload while working on the UI:

```bash
docker compose -f docker-compose.yml -f docker-compose.dev.yml up --build
```

## What a change needs

- **A test.** Anything touching Ansible, the network or a model gets a mock —
  see the existing files in `backend/tests/` for the pattern.
- **`make test` green in the image**, and `python -m mypy app --config-file mypy.ini`
  clean.
- **A CHANGELOG entry** under a version heading. `tests/test_version.py` fails the
  build if `app.__version__` has no matching section.

## Things worth knowing before you start

- **Migrations are append-only.** Add a `_mNNN_*` function to
  `app/models/migrations.py`, append it to `MIGRATIONS`, bump `SCHEMA_VERSION`.
  Never edit or renumber a released step — someone's database is already stamped
  with it. Every step must be idempotent, because unstamped pre-0.1.0 databases
  replay all of them.
- **New API routes need a capability.** `app/core/authz.py` maps every route to
  one; a route with no entry denies everyone, and `tests/test_authz.py` enumerates
  the real route table so an unmapped route fails the build rather than shipping.
- **The WebSocket authenticates itself.** HTTP middleware doesn't cover WS scope.
  If you add a socket, check the session *and* the role in the handler.
- **Secrets never leave the API.** Credential material is read from disk at run
  time and written to 0600 files in a per-run temp dir. No endpoint returns one.

## Style

Comments explain *why*, not *what* — the code already says what. If a decision
looks odd, the comment should say what would break if it were done the obvious
way. Prefer a measurement over an assertion: "2^16 costs 105ms/64MB here" beats
"this is fast enough".

Keep the register plain and technical. No marketing language.

## Reporting a bug

Open an issue with the version from `curl -s localhost:8765/health`, what you
expected, what happened, and the relevant lines from `docker compose logs app`.

Security issues go through
[GitHub Security Advisories](https://github.com/mar0ls/playforge/security/advisories/new)
instead — see [SECURITY.md](SECURITY.md).
