"""Anti-hallucination layer for AI-generated Ansible explanations.

Two checks, both deterministic and side-effect-free:

1. **Module references** — extract fully-qualified Ansible module names
   (`namespace.collection.module`) from the text and verify each exists in the
   container's `ansible-doc -l` output. Catches the most common hallucination:
   plausible-sounding but non-existent modules.

2. **Confidence scoring** — `high` if no unknown modules; `medium` if 1 unknown;
   `low` if ≥2 unknown OR self-critique surfaced unsupported claims (the
   second-pass LLM check is run from `core/ai.py` and merged in there).

The list of known modules is cached process-wide via `lru_cache` — generating it
calls Ansible plugin loaders and takes seconds the first time, which would be a
real UX hit if we did it per-request.
"""
from __future__ import annotations

import re
import subprocess
from functools import lru_cache


# Match exactly-three-segment `namespace.collection.module` tokens. The negative
# lookbehind/lookahead pair excludes anything embedded in a filesystem path
# (`/etc/yum.repos.d/`) or a longer dotted chain. Requiring exactly 3 segments
# (not 2+) avoids matching registered-var dotted access like `result.stat.exists`.
_FQ_MODULE_RE = re.compile(r"(?<![/\w.])([a-z][a-z0-9_]*)\.[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*(?![./\w])")

# A token shaped like `a.b.c` is only treated as a module if its FIRST segment is a
# real Ansible collection namespace. This is what separates `community.general.ufw`
# (a module) from `server.example.com` / `certs.tar.gz` / `smtp.example.com` (an FQDN
# or filename) — the battery run showed those being mis-reported as modules.
_KNOWN_NAMESPACES = {
    "ansible", "community", "kubernetes", "amazon", "google", "azure", "cisco",
    "arista", "junipernetworks", "fortinet", "dellemc", "netapp", "purestorage",
    "vmware", "openstack", "theforeman", "grafana", "prometheus", "hashi_vault",
    "containers", "chocolatey", "infoblox", "f5networks", "check_point", "cyberark",
    "ovirt", "servicenow", "splunk", "sensu", "lowlydba", "awx",
}

# Real `ansible.builtin.*` modules that `ansible-doc -l` may NOT list because they're
# only loadable on a matching platform (e.g. yum/dnf aren't listed on a Debian image).
# These exist in ansible-core — calling them "doesn't exist" would be wrong and
# misleading. We treat them as platform-conditional, not hallucinated.
_PLATFORM_BUILTINS = {
    "ansible.builtin.yum", "ansible.builtin.dnf", "ansible.builtin.dnf5",
    "ansible.builtin.apt", "ansible.builtin.apt_key", "ansible.builtin.apt_repository",
    "ansible.builtin.yum_repository", "ansible.builtin.rpm_key", "ansible.builtin.zypper",
    "ansible.builtin.package", "ansible.builtin.service", "ansible.builtin.systemd",
    "ansible.builtin.systemd_service", "ansible.builtin.sysvinit", "ansible.builtin.hostname",
}


@lru_cache(maxsize=1)
def known_modules() -> frozenset[str]:
    """Cache the FQ module list returned by `ansible-doc -l`. Empty set if the
    binary isn't on PATH or the call times out — caller treats empty as 'skipped'."""
    try:
        out = subprocess.check_output(
            ["ansible-doc", "-l", "-t", "module"],
            text=True, timeout=60, stderr=subprocess.DEVNULL,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, subprocess.CalledProcessError):
        return frozenset()
    modules: set[str] = set()
    for line in out.splitlines():
        if not line or line.startswith(" "):
            continue
        name = line.split()[0]
        if "." in name:  # only collect fully-qualified names
            modules.add(name)
    return frozenset(modules)


def validate_text(text: str) -> dict:
    """Run deterministic validation checks on AI-generated text.

    A module not found by `ansible-doc` is split into two very different cases:
      * `invalid_modules` — `ansible.builtin.X` that doesn't exist. ansible-core
        always ships the complete builtin set, so this is a genuine error
        (e.g. `ansible.builtin.ufw` — ufw lives in `community.general`).
      * `uninstalled_modules` — a plausible module from another namespace
        (`community.*`, `ansible.posix`, ...) we can't see only because that
        collection isn't installed here. NOT a hallucination — it just needs
        `ansible-galaxy collection install`.

    `unknown_modules` is kept (= both lists) for backward compatibility.
    """
    out = {
        "modules_mentioned": [],
        "unknown_modules": [],
        "invalid_modules": [],
        "uninstalled_modules": [],
        "checked_modules": False,
        "confidence": "high",
    }
    if not text:
        return out

    modules = known_modules()

    # Keep a token only if it really looks like a module: either it's an installed
    # module, or its first segment is a known collection namespace. This drops FQDNs
    # / filenames / dotted var access (`server.example.com`, `certs.tar.gz`,
    # `result.stat.exists`) that the `a.b.c` shape would otherwise catch.
    candidates = set()
    for match in _FQ_MODULE_RE.finditer(text):
        token = match.group(0)
        ns = match.group(1)
        if token in modules or ns in _KNOWN_NAMESPACES:
            candidates.add(token)
    mentioned = sorted(candidates)
    out["modules_mentioned"] = mentioned

    if not modules:
        out["note"] = "ansible-doc unavailable in container; module validation skipped"
        return out

    out["checked_modules"] = True
    invalid, uninstalled = [], []
    for m in mentioned:
        if m in modules or m in _PLATFORM_BUILTINS:
            continue                 # exists (or exists but not loadable on this OS)
        if m.startswith("ansible.builtin."):
            invalid.append(m)        # genuinely not a builtin → real error
        else:
            uninstalled.append(m)    # likely just an uninstalled collection
    out["invalid_modules"] = invalid
    out["uninstalled_modules"] = uninstalled
    out["unknown_modules"] = invalid + uninstalled

    # Only genuinely-invalid modules hurt confidence; an uninstalled collection
    # is an actionable note, not a hallucination.
    if len(invalid) >= 2:
        out["confidence"] = "low"
    elif len(invalid) == 1:
        out["confidence"] = "medium"
    return out


def downgrade_confidence(validation: dict, *, unsupported_claims: int) -> None:
    """Re-grade `validation['confidence']` after the LLM self-critique completes.

    Self-critique findings are integrated here rather than inside `validate_text`
    so the deterministic layer stays cheap and synchronous.
    """
    if unsupported_claims >= 2:
        validation["confidence"] = "low"
    elif unsupported_claims == 1 and validation.get("confidence") == "high":
        validation["confidence"] = "medium"
