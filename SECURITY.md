# Security

## Reporting a vulnerability

Report privately through GitHub Security Advisories on
[mar0ls/playforge](https://github.com/mar0ls/playforge/security/advisories/new).
Please don't open a public issue for anything exploitable.

## What Playforge is today

**Single-tenant.** One shared password, one operator. There are no user accounts,
no roles, and no per-user audit trail. Treat access to the UI as equivalent to
shell access on the machine running the container, because that is what it is:

- Runs execute on the app host, as the app user, with the credentials from the
  vault. Anyone who can log in can run an arbitrary playbook against any host the
  controller can reach.
- The AI agent can write files and run playbooks when those capabilities are
  enabled. It's bounded by the same trust boundary, not a smaller one.

Deploy it on a trusted network, behind TLS, not on the public internet.

Real multi-user with roles is planned, on top of run isolation — roles mean
little while every run can do anything on the controller.

### Run isolation (optional, off by default)

Setting `run.isolation` (or `ANSIBLE_GUI_RUN_ISOLATION=1`) runs playbooks in a
sandbox instead of directly on the app host. With the default `bwrap` mechanism
only `/bin`, `/etc`, `/usr`, `/opt` (read-only) and the project's own directory
are visible, so a run can't read `/data` — no `master.key`, no `app.db`, no other
project's repository.

It's off by default because it depends on the host allowing unprivileged user
namespaces, and a run that refuses to start is worse than one that isn't
sandboxed. Turn it on and confirm a run still works before relying on it.

`docker`/`podman` are also accepted, giving a container per run — but they need
the engine's socket inside the app container, which is root-equivalent access to
the host. That buys run isolation and loses considerably more elsewhere. Prefer
`bwrap`.

## What is enforced

Verified behaviour, not aspiration:

| Area | Behaviour |
|---|---|
| Auth | Optional. `ANSIBLE_GUI_PASSWORD` unset = **no login at all**. Set = every request needs a valid session. |
| Session | HMAC-signed expiring token, `HttpOnly`, `SameSite=Lax`, `Secure` when served over HTTPS. Signing key derives from the password *and* the credential master key, so a leaked cookie can't be forged with either alone. |
| Login throttling | 5 failed attempts per client address triggers a lockout, doubling from 30s to a 15min cap. Fails closed: the correct password is refused while locked. |
| WebSockets | The run WebSocket re-checks the session itself — HTTP middleware doesn't cover the WS scope, so without this a LAN attacker could open a socket and run playbooks. |
| Credentials | Encrypted at rest with Fernet. The key lives at `/data/master.key` (0600) or comes from `ANSIBLE_GUI_MASTER_KEY`. Secrets are never returned by the API. |
| Run secrets | Vault/become passwords and SSH keys are written to 0600 files in a per-run temp dir, which is deleted after the run. |
| Project files | Every path is resolved and confined to the project directory; `..` and absolute paths are refused. |
| Rendered output | Model and file content is HTML-escaped before any Markdown formatting is re-introduced, so a prompt-injected reply can't inject script. |
| Headers | CSP, `X-Frame-Options: DENY`, `nosniff`, `no-referrer` on every response. |

### CSP caveat

`script-src` allows `'unsafe-inline'` and `'unsafe-eval'`. Every page template
carries an inline `<script>` block and the vendored htmx builds handlers with
`Function()`/`eval()`, so removing them blanks the UI. The policy still blocks
loading script from another origin, and `connect-src 'self'` means injected
script has nowhere to exfiltrate to. Tightening this needs the inline blocks
moved into files and a nonce; it isn't done yet.

## Backups hold your secrets

`scripts/backup.sh` writes an archive containing `master.key` and your project
repositories. It's created 0600 and `backups/` is gitignored, but it is
plaintext at rest — encrypt it before moving it anywhere:

```bash
gpg --symmetric --cipher-algo AES256 backups/playforge-backup-*.tar.gz
```

Losing `master.key` means the encrypted credentials cannot be recovered.

## Supported versions

Pre-1.0: only the latest release gets fixes.
