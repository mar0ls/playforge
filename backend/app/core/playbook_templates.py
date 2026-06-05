"""A small curated library of starter playbooks.

Each entry's `spec` is in the exact shape `playbook_builder.build_playbook`
consumes, so the UI can drop it straight into the builder form and the tests can
validate every template by building it. Vars are referenced as `{{ jinja }}` so a
template is a starting point, not a finished playbook — the user fills the blanks.

Keep this list short and high-signal: the goal is to bridge "empty file" and
"advanced playbook", not to mirror Galaxy.
"""
from __future__ import annotations

CATALOG: list[dict] = [
    {
        "id": "blank",
        "name": "Blank playbook",
        "description": "An empty single play to build on from scratch — one host group, one empty task.",
        "category": "Basics",
        "spec": {
            "name": "New play", "hosts": "all", "become": False,
            "tasks": [
                {"name": "First task", "module": "ansible.builtin.debug",
                 "args_yaml": "msg: hello", "tags": []},
            ],
        },
    },
    {
        "id": "ping",
        "name": "Ping all hosts",
        "description": "Verify connectivity and a working Python interpreter on every host.",
        "category": "Basics",
        "spec": {
            "name": "Ping hosts", "hosts": "all", "become": False, "gather_facts": False,
            "tasks": [
                {"name": "Ping", "module": "ansible.builtin.ping", "args_yaml": "", "tags": ["check"]},
            ],
        },
    },
    {
        "id": "apt-upgrade",
        "name": "Update & upgrade (Debian/Ubuntu)",
        "description": "Refresh apt cache and apply a full dist-upgrade on Debian-family hosts.",
        "category": "Maintenance",
        "spec": {
            "name": "Apt update and upgrade", "hosts": "all", "become": True,
            "tasks": [
                {"name": "Update cache and upgrade", "module": "ansible.builtin.apt",
                 "args_yaml": "update_cache: true\nupgrade: dist\ncache_valid_time: 3600",
                 "when": "ansible_os_family == 'Debian'", "tags": ["update"]},
            ],
        },
    },
    {
        "id": "dnf-upgrade",
        "name": "Update packages (RHEL/Fedora)",
        "description": "Bring all packages to their latest version on RedHat-family hosts.",
        "category": "Maintenance",
        "spec": {
            "name": "DNF update", "hosts": "all", "become": True,
            "tasks": [
                {"name": "Upgrade all packages", "module": "ansible.builtin.dnf",
                 "args_yaml": "name: '*'\nstate: latest",
                 "when": "ansible_os_family == 'RedHat'", "tags": ["update"]},
            ],
        },
    },
    {
        "id": "create-user",
        "name": "Create a user with SSH key",
        "description": "Create a login user and authorize an SSH public key. Set deploy_pubkey.",
        "category": "Provisioning",
        "spec": {
            "name": "Create deploy user", "hosts": "all", "become": True,
            "tasks": [
                {"name": "Create user", "module": "ansible.builtin.user",
                 "args_yaml": "name: deploy\nshell: /bin/bash\ncreate_home: true\nstate: present",
                 "tags": ["users"]},
                {"name": "Authorize SSH key", "module": "ansible.builtin.authorized_key",
                 "args_yaml": "user: deploy\nstate: present\nkey: \"{{ deploy_pubkey }}\"",
                 "tags": ["users"]},
            ],
        },
    },
    {
        "id": "install-docker",
        "name": "Install Docker (Debian/Ubuntu)",
        "description": "Install the distro docker.io package and enable the service.",
        "category": "Provisioning",
        "spec": {
            "name": "Install Docker", "hosts": "all", "become": True,
            "tasks": [
                {"name": "Install docker.io", "module": "ansible.builtin.apt",
                 "args_yaml": "name: docker.io\nstate: present\nupdate_cache: true",
                 "when": "ansible_os_family == 'Debian'", "tags": ["docker"]},
                {"name": "Enable and start docker", "module": "ansible.builtin.service",
                 "args_yaml": "name: docker\nstate: started\nenabled: true",
                 "tags": ["docker"]},
            ],
        },
    },
    {
        "id": "deploy-git",
        "name": "Deploy from git + restart service",
        "description": "Check out an app from git and restart its systemd unit. Set app_repo/app_dir/app_service.",
        "category": "Deployment",
        "spec": {
            "name": "Deploy app", "hosts": "all", "become": True,
            "tasks": [
                {"name": "Check out repository", "module": "ansible.builtin.git",
                 "args_yaml": "repo: \"{{ app_repo }}\"\ndest: \"{{ app_dir }}\"\nversion: \"{{ app_version | default('main') }}\"",
                 "tags": ["deploy"]},
                {"name": "Restart service", "module": "ansible.builtin.systemd",
                 "args_yaml": "name: \"{{ app_service }}\"\nstate: restarted",
                 "tags": ["deploy"]},
            ],
        },
    },
]

_BY_ID = {t["id"]: t for t in CATALOG}


def catalog() -> list[dict]:
    """The full library — metadata plus each ready-to-edit spec."""
    return CATALOG


def get(template_id: str) -> dict | None:
    return _BY_ID.get(template_id)
