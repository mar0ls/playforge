# Playforge

A self-hosted web UI for managing and running Ansible — a simpler, friendlier AWX
that runs from a single `docker compose up`, with no Postgres, Redis or Receptor.

What makes it different is the **AI layer that checks its own output**: it
generates playbooks from plain language, then verifies them against `ansible-doc`
for modules that don't exist and against a rule engine for the logic mistakes a
model misses — SSH/UFW lockout, destructive operations, malformed `vars`. Works
fully offline against a local Ollama.

Source, issues and full documentation: **https://github.com/mar0ls/playforge**

## Tags

| Tag | Meaning |
|---|---|
| `latest` | Newest stable release. Never moves to a prerelease. |
| `0.9`, `1.0`, … | Newest patch within that minor. |
| `0.9.0` | Exact version. Use this for reproducible deployments. |
| `0.9.0-rc1` | Release candidate. Published on its own; never moves `latest`. |

Built for `linux/amd64` and `linux/arm64`.

## Run it

One file, no clone needed. Save as `docker-compose.yml`:

```yaml
services:
  app:
    image: mar0ls/playforge:latest
    container_name: playforge
    ports:
      - "8765:8765"
    volumes:
      # Projects, git repos, SQLite and the credential master key live here.
      - ./data:/data
    environment:
      ANSIBLE_GUI_DATA_DIR: /data
      ANSIBLE_GUI_HOST: 0.0.0.0
      # First administrator, created on first start. Leave both empty and a
      # one-time setup token is printed to the log instead.
      ANSIBLE_GUI_ADMIN_USER: ${ANSIBLE_GUI_ADMIN_USER:-}
      ANSIBLE_GUI_ADMIN_PASSWORD: ${ANSIBLE_GUI_ADMIN_PASSWORD:-}
      # Optional: AI helper. Also configurable at runtime under Settings.
      ANTHROPIC_API_KEY: ${ANTHROPIC_API_KEY:-}
      OPENAI_API_KEY: ${OPENAI_API_KEY:-}
      OLLAMA_URL: ${OLLAMA_URL:-}
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "curl", "-fsS", "http://127.0.0.1:8765/health"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 30s
```

```bash
docker compose up -d          # → http://127.0.0.1:8765
```

## First sign-in

If you didn't set `ANSIBLE_GUI_ADMIN_USER` / `ANSIBLE_GUI_ADMIN_PASSWORD`, the
container prints a one-time setup token:

```bash
docker compose logs app | grep "SETUP TOKEN"
```

Open `/setup` and paste it. The token exists because the port is published on
`0.0.0.0` — without it, anyone who reached the port before you did could claim
the instance. It changes on every restart and disappears once an account exists.

Roles are **admin**, **operator** and **viewer**: viewer reads, operator also runs
playbooks and edits files, admin also manages credentials, users and settings.

## Upgrading

```bash
docker compose pull && docker compose up -d
```

Schema migrations run at startup, so skipping several versions is fine.
Downgrading is not supported — an older image against a newer database logs a
warning rather than half-working.

Back up first. `./data` holds `master.key`, and **without it the stored
credentials cannot be recovered**:

```bash
tar -czf playforge-backup.tar.gz data/
```

The repository ships `scripts/backup.sh`, which takes a consistent snapshot while
the app is running.

## Environment

| Variable | Purpose |
|---|---|
| `ANSIBLE_GUI_ADMIN_USER` / `_PASSWORD` | Create the first admin at startup. Each also accepts a `_FILE` form pointing at a mounted secret. |
| `ANSIBLE_GUI_PASSWORD` | Legacy single shared password, used when no accounts exist. |
| `ANSIBLE_GUI_MASTER_KEY` | Credential encryption key. Generated into `/data/master.key` if unset. |
| `ANSIBLE_GUI_RUN_ISOLATION` | `1` sandboxes runs so a playbook can't read `/data`. Needs two `security_opt` lines — see SECURITY.md. |
| `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `OLLAMA_URL` | AI backend. All optional; the app works without any. |

## Security

Treat operator access as shell access on the host running the container: a run
executes there with the credentials from the vault. Deploy on a trusted network,
behind TLS, not on the public internet. Full model:
[SECURITY.md](https://github.com/mar0ls/playforge/blob/main/SECURITY.md).

GPL-3.0.
