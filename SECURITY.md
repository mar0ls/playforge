# Security

## Reporting a vulnerability

Report privately through GitHub Security Advisories on
[mar0ls/playforge](https://github.com/mar0ls/playforge/security/advisories/new).
Please don't open a public issue for anything exploitable.

## What Playforge is today

**Accounts and roles, or a single shared password.** Which one is in effect
depends on whether any accounts exist:

| Accounts | `ANSIBLE_GUI_PASSWORD` | Behaviour |
|---|---|---|
| yes | — | Sign in with a username; the role decides what you may call |
| no | set | One shared password, full access, as before |
| no | unset | No authentication at all |

Roles are `admin`, `operator` and `viewer`. Every API route maps to a required
capability, an unmapped route denies everyone, and a test enumerates the real
route table so a new endpoint can't ship without that decision.

Even so, treat `operator` and above as equivalent to shell access on the machine
running the container, because that is what it is: a run executes on the app host
with the credentials from the vault. Run isolation (below) is what narrows this,
and it is off by default.

- Runs execute on the app host, as the app user, with the credentials from the
  vault. Anyone who can log in can run an arbitrary playbook against any host the
  controller can reach.
- The AI agent can write files and run playbooks when those capabilities are
  enabled. It's bounded by the same trust boundary, not a smaller one.

Deploy it on a trusted network, behind TLS, not on the public internet.

### Run isolation (optional, off by default)

Setting `run.isolation` (or `ANSIBLE_GUI_RUN_ISOLATION=1`) runs playbooks in a
sandbox instead of directly on the app host. With the default `bwrap` mechanism
only `/bin`, `/etc`, `/usr`, `/opt` (read-only) and the project's own directory
are visible, so a run can't read `/data` — no `master.key`, no `app.db`, no other
project's repository.

**The container needs two flags for this to work at all:**

```yaml
security_opt:
  - seccomp=unconfined
  - systempaths=unconfined
```

Docker's default seccomp profile blocks the user-namespace clone the sandbox
needs, and its masked `/proc` paths block the mount it always performs. Measured
on Docker 29.4.0: neither running as root nor adding `CAP_SYS_ADMIN` helps, and
`--privileged` works but defeats the point. Both flags are required; nothing else
is.

It fails closed: with the setting on and the flags missing, runs fail rather than
quietly running unsandboxed.

Off by default because those flags are a real trade, not a free win. They thin
the barrier between the app container and the host kernel (no syscall filter,
unmasked `/proc`) in order to put a barrier between a *run* and your data. Which
matters more depends on whether you're more worried about a bad playbook reading
your credential vault, or about a container escape. Decide deliberately.

`docker`/`podman` are also accepted, giving a container per run — but they need
the engine's socket inside the app container, which is root-equivalent access to
the host. That buys run isolation and loses considerably more elsewhere. Prefer
`bwrap`.

## What is enforced

Verified behaviour, not aspiration:

| Area | Behaviour |
|---|---|
| Auth | Accounts when any exist, else the shared password, else open. See the table above. |
| First admin | From `ANSIBLE_GUI_ADMIN_USER`/`_PASSWORD` (or their `_FILE` form) at startup, or via `/setup` guarded by a one-time token printed to the container log. The token closes the window where anyone reaching the published port could claim the instance before the operator does. |
| Roles | Per-route capability check, fail-closed on unmapped routes. Enforced on the WebSocket too, which HTTP middleware doesn't cover. |
| Revocation | The account is re-read on every request, so disabling or deleting one ends its sessions immediately rather than at token expiry. |
| Passwords | scrypt (N=2^16, r=8, p=1) with per-password salt; cost parameters stored in the hash and upgraded on next sign-in. A missing account costs the same time as a wrong password, so timing can't enumerate usernames. |
| Session | HMAC-signed expiring token, `HttpOnly`, `SameSite=Lax`, `Secure` when served over HTTPS. Signing key derives from the password *and* the credential master key, so a leaked cookie can't be forged with either alone. |
| Login throttling | 5 failed attempts per client address triggers a lockout, doubling from 30s to a 15min cap. Fails closed: the correct password is refused while locked. |
| WebSockets | The run WebSocket re-checks both the session and the role itself — HTTP middleware doesn't cover the WS scope, so without this a LAN attacker could open a socket and run playbooks, and a viewer could bypass the capability check on `POST /api/runs` by using the socket instead. |
| Credentials | Encrypted at rest with Fernet. The key lives at `/data/master.key` (0600) or comes from `ANSIBLE_GUI_MASTER_KEY`. Secrets are never returned by the API. |
| Run secrets | Vault/become passwords and SSH keys are written to 0600 files in a per-run temp dir, which is deleted after the run. |
| Project files | Every path is resolved and confined to the project directory; `..` and absolute paths are refused. |
| Rendered output | Model and file content is HTML-escaped before any Markdown formatting is re-introduced, so a prompt-injected reply can't inject script. |
| Headers | CSP, `X-Frame-Options: DENY`, `nosniff`, `no-referrer` on every response. |

### No CSRF tokens

State-changing requests are protected by `SameSite=Lax` on the session cookie,
which stops a cross-site form or fetch from carrying it, and by `form-action
'self'` in the CSP. There are no per-request CSRF tokens, so that cookie
attribute is the whole defence — an older browser that ignores `SameSite`, or a
same-site subdomain an attacker controls, would not be covered. Tokens are
planned.

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
