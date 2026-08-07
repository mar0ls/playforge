"""Playforge — a self-hosted web UI for managing and running Ansible.

`__version__` is the single source of truth for the running version. It is
surfaced in three places so a user can always tell what they're on:
the OpenAPI schema (`/docs`), the `/health` payload, and the sidebar footer.
Bump it together with the git tag and the CHANGELOG entry.
"""

__version__ = "0.8.0"
