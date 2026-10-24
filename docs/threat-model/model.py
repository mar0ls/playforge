"""Playforge threat model, as pytm (https://github.com/izar/pytm) code.

This is a structured, checkable companion to ../../SECURITY.md, not a replacement
for it: SECURITY.md is the prose explanation of why each boundary is drawn where it
is; this file is the versioned, diffable description of the same boundaries, so a
change to a trust assumption (a dataflow losing authentication, a sandbox being
removed) shows up as a review comment on this file instead of only living in
someone's head.

Usage (from this directory, with pytm installed -- see requirements.txt):

    python3 model.py --dfd | dot -Tsvg -o dfd.svg       # data-flow diagram
    python3 model.py --seq > sequence.puml              # sequence diagram (PlantUML)
    python3 model.py --json model.json                  # machine-readable dump
    python3 summarize.py model.json > summary.md        # human-readable summary
    python3 model.py --list                             # every threat rule pytm knows

pytm 1.4.0 quirk worth knowing before editing this file: `Dataflow.protocol`,
`.srcPort`/`.dstPort`, `.controls.isEncrypted` and `.controls.authenticatesDestination`
are NOT read back from what you pass into the Dataflow() constructor. During
`tm.check()`, `_apply_forward_defaults()` overwrites them from the *endpoint
elements*: protocol/dstPort/isEncrypted come from the flow's sink,
authenticatesDestination comes from the flow's source. Those five fields are set on
the Server/Datastore elements below, not on the Dataflow calls. Every other Controls
flag (implementsPOLP, validatesInput, sanitizesInput, authorizesSource,
handlesResourceConsumption, ...) does belong on the Dataflow, which is where the
assertions below are set.
"""

from pytm import (
    TM,
    Actor,
    Boundary,
    Controls,
    Dataflow,
    Datastore,
    DatastoreType,
    Server,
)

tm = TM("Playforge")
tm.description = (
    "Self-hosted web UI for managing and running Ansible. A single container "
    "(FastAPI + SQLite + an in-process scheduler) serves the UI, stores encrypted "
    "credentials and one git repo per project, runs playbooks against a fleet over "
    "SSH, and talks to a pluggable AI backend (Anthropic / OpenAI / local Ollama) "
    "that self-checks its output. See SECURITY.md for the prose version of these "
    "trust boundaries."
)

# ---------------------------------------------------------------------------
# Boundaries
# ---------------------------------------------------------------------------

operator_zone = Boundary("Operator network (browser)")
app_container = Boundary("App container (control plane: app, SQLite, vault, repos)")
# Deliberately lower trust: SECURITY.md is explicit that a run executes on the app
# host with vault credentials and reaches any host the controller can — and that
# optional run isolation exists precisely because a playbook (or a compromised
# target) is not assumed benign.
managed_fleet = Boundary("Managed fleet (target hosts over SSH)")
# External by definition: a cloud model is off-box; even local Ollama is a separate
# process reached over HTTP. Replies are untrusted input (prompt injection).
ai_provider_zone = Boundary("AI provider (Anthropic / OpenAI / Ollama)")
git_remote_zone = Boundary("Git remote (Gitea / GitHub / self-hosted)")

# ---------------------------------------------------------------------------
# Actors, processes, datastores
#
# protocol/port/isEncrypted/authenticatesDestination live here (see the module
# docstring), so the app server's own controls are what inbound flows inherit as
# their in-transit flags.
# ---------------------------------------------------------------------------

operator = Actor(
    "Operator (browser)",
    inBoundary=operator_zone,
    controls=Controls(authenticatesDestination=True),  # browser TLS validation
)

app = Server(
    "Playforge app (FastAPI/uvicorn)",
    inBoundary=app_container,
    OS="Linux",
    protocol="HTTPS",
    port=443,
    controls=Controls(
        isEncrypted=True,  # TLS via a fronting proxy — SECURITY.md: "behind TLS, not public internet"
        authenticatesDestination=True,  # SSH host-key checking left on by default (ansible.cfg scaffold)
        implementsAuthenticationScheme=True,  # accounts or shared password; signed session cookie, core/auth.py
        hasAccessControl=True,  # RBAC admin/operator/viewer, fail-closed on unmapped routes, core/authz.py
        validatesInput=True,
    ),
)

app_db = Datastore(
    "SQLite (app.db)",
    inBoundary=app_container,
    type=DatastoreType.SQL,
    storesSensitiveData=True,  # password hashes, AI keys (Fernet-sealed), run history
    storesLogData=True,
    controls=Controls(
        isEncryptedAtRest=False,  # the volume is the operator's responsibility; secrets in it are field-encrypted
        hasAccessControl=True,
    ),
)

vault = Datastore(
    "Credential vault (/data/master.key + credentials/)",
    inBoundary=app_container,
    type=DatastoreType.FILE_SYSTEM,
    storesSensitiveData=True,  # SSH keys, SSH/become/vault passwords, WireGuard keys
    controls=Controls(
        isEncryptedAtRest=True,  # Fernet; key at /data/master.key (0600) or ANSIBLE_GUI_MASTER_KEY
        hasAccessControl=True,
    ),
)

repos = Datastore(
    "Project git repos (/data/projects/<id>)",
    inBoundary=app_container,
    type=DatastoreType.FILE_SYSTEM,
    storesSensitiveData=True,  # playbooks, inventories, vault files, run artifacts
    controls=Controls(hasAccessControl=True),
)

target = Server(
    "Managed target host",
    inBoundary=managed_fleet,
    OS="Linux",
    protocol="SSH",
    port=22,
    controls=Controls(
        isEncrypted=True,   # the SSH transport
        isHardened=False,   # the fleet host is what's being managed, not vetted infra
    ),
)

ai = Server(
    "AI provider endpoint",
    inBoundary=ai_provider_zone,
    protocol="HTTPS",
    port=443,
    controls=Controls(isEncrypted=True),
)

remote = Server(
    "Git remote",
    inBoundary=git_remote_zone,
    protocol="HTTPS",
    port=443,
    controls=Controls(isEncrypted=True),
)

# ---------------------------------------------------------------------------
# Dataflows — each `note` cites the code (and, where one exists, the regression
# test) that its control flags assert against.
# ---------------------------------------------------------------------------

Dataflow(
    operator,
    app,
    "Sign-in (username/password or shared password) and state-changing requests",
    controls=Controls(
        implementsAuthenticationScheme=True,
        handlesResourceConsumption=True,  # per-IP login throttle, core/auth.py
        authorizesSource=True,            # per-route capability check, core/authz.py
    ),
    note=(
        "core/auth.py (scrypt N=2^16, timing-equalised, signed session cookie tied "
        "to the master key), core/authz.py (fail-closed RBAC, enforced on the WS too), "
        "core/csrf.py (signed-token + Origin double guard, all three auth modes). "
        "Every response carries a CSP; hardened 2026-10-23 to drop 'unsafe-inline' "
        "from script-src in favour of a per-request nonce (main.py _csp + csp_nonce), "
        "so an injected <script> without the nonce can't run. 'unsafe-eval' stays "
        "because Alpine/htmx evaluate with new Function. Account lifecycle and "
        "AI-provider/key changes (the admin surface) are written to the audit log "
        "with the acting user — added 2026-10-23 (api/users.py, api/ai.py, "
        "core/audit.py; the log is capped and pruned, and surfaced read-only at "
        "/audit for admins). Tests: test_auth, test_login_throttle, test_authz, "
        "test_csrf, test_auth_middleware, test_csp, test_audit."
    ),
)

Dataflow(
    operator,
    repos,
    "Import a project (local path / uploaded .zip / git clone)",
    controls=Controls(
        validatesInput=True,            # zip-entry traversal + clone-URL allowlist
        handlesResourceConsumption=True,  # zip size/entry caps
    ),
    note=(
        "api/projects.py import_path/import_zip/import_git. Import is the widest "
        "input in the API. Hardened 2026-10-23: (1) import_git/git_set_remote now run "
        "_validate_clone_url, refusing ext:: / fd:: transport helpers (arbitrary "
        "command execution) and file:// / bare local paths (clone from the server's "
        "own filesystem); (2) import_zip caps upload size, declared expanded size and "
        "entry count, so a zip bomb can't exhaust the data volume; (3) "
        "import_directory now copies symlinks AS links (symlinks=True) instead of "
        "dereferencing them, so a link to /etc/shadow or master.key can't smuggle its "
        "target's content into a readable project. Tests: "
        "test_projects_api_import (clone-URL allowlist, zip-expansion cap, traversal), "
        "test_storage (symlink preserved + _resolve_safe blocks reading it)."
    ),
)

Dataflow(
    app,
    repos,
    "Read / write / lint / parse project files; auto-commit on every change",
    controls=Controls(
        validatesInput=True,   # path sandbox; executable inventory refused on parse
        sanitizesInput=True,
    ),
    note=(
        "core/storage.py _resolve_safe confines every path to the project dir and "
        "refuses .. , absolute paths and anything under .git (the repo auto-commits, "
        "so a planted hook/config would be code execution). Fixed 2026-10-23: "
        "core/inventory.parse() refused to parse an *executable* inventory source. "
        "Ansible runs an executable inventory file as a dynamic-inventory script, and "
        "the parse endpoint is `read` capability (fired automatically when the "
        "Inventory tab opens), so a crafted executable file in an imported project "
        "was arbitrary code execution as the app user — outside run isolation and "
        "below the operator trust boundary. Genuine dynamic inventory still works at "
        "run time (separate subprocess, gated by `run`). Tests: test_inventory "
        "(executable file + executable-in-directory both refused, marker never "
        "written), test_storage (_resolve_safe)."
    ),
)

Dataflow(
    app,
    target,
    "Playbook / ad-hoc run over SSH, with credentials injected from the vault",
    controls=Controls(
        implementsPOLP=True,  # optional bwrap sandbox hides /data from the run
        isHardened=True,      # secrets written to 0600 files in a per-run temp dir, deleted after
    ),
    note=(
        "core/runner.py. Credentials become 0600 files / extravars in a per-run "
        "private_data_dir that is removed after the run (no vault/ssh material left "
        "behind). SECURITY.md is explicit that operator+ is effectively shell access "
        "on the app host; run.isolation (ANSIBLE_GUI_RUN_ISOLATION, bwrap) narrows a "
        "run so it can't read /data (master.key, app.db, other projects), fails "
        "closed, and is off by default because it needs two security_opt lines. "
        "Tests: test_run_isolation, test_runner_summary, test_adhoc_credentials."
    ),
)

Dataflow(
    app,
    ai,
    "AI chat / generate / agent prompts; model replies are untrusted input",
    controls=Controls(
        sanitizesInput=True,    # replies HTML-escaped before Markdown render (no injected <script>)
        authorizesSource=True,  # agent capabilities are opt-in tiers; web_fetch is domain-allowlisted
        validatesInput=True,    # anti-hallucination + rule engine gate generated playbooks
    ),
    note=(
        "core/ai/*, core/agent.py, core/agent_tools.py. A reply is attacker-shaped "
        "text (prompt injection), so it is HTML-escaped before Markdown formatting is "
        "re-introduced. The agent's tools are tiered READ -> MUTATE -> CONFIRM and "
        "each tier is opt-in; web_fetch is restricted to docs.ansible.com / "
        "galaxy.ansible.com, never an arbitrary URL. Generated YAML is checked against "
        "ansible-doc (hallucinated modules) and the rule engine (lockout, destructive "
        "ops) before it is trusted. Every state-changing tool the agent runs "
        "(write_file, move, mkdir, galaxy_add, delete, run_playbook, web_fetch) is "
        "written to the audit log, attributed to the requesting user — added "
        "2026-10-23 (api/ai.py _audit_agent_steps, core/audit.py). Tests: "
        "test_ai_validate, test_agent, test_agent_escapes, test_agent_tools, "
        "test_golden_rules, test_audit."
    ),
)

Dataflow(
    app,
    vault,
    "Credential and AI-key reads/writes",
    controls=Controls(
        validatesInput=True,
        implementsPOLP=True,  # secrets never returned by the API; `secrets` capability gates use
    ),
    note=(
        "core/credentials.py, core/settings_store.py. Secrets are Fernet-sealed with "
        "the master key and are never serialised back to a client (the API reports "
        "has_secret, not the value); using or changing one needs the `secrets` "
        "capability. Tests: test_credentials_api, test_settings_store, test_vault."
    ),
)

Dataflow(
    app,
    remote,
    "Git push / pull with a per-operation token spliced into the URL",
    controls=Controls(
        validatesInput=True,      # stored remote URL is allowlisted (no ext::/file://)
        providesIntegrity=True,   # pull is --ff-only, never a silent merge of divergent history
    ),
    note=(
        "core/storage.py git_push/git_pull/_auth_url, api/projects.py git_set_remote. "
        "A username/token is spliced in for one operation and never persisted or "
        "logged (errors replace the auth URL with the bare one). git_set_remote now "
        "runs _validate_clone_url too, since a stored remote is reused verbatim by "
        "push/pull. Tests: test_git_sync, test_projects_api_galaxy_git."
    ),
)

if __name__ == "__main__":
    tm.process()
