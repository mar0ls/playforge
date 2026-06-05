"""Extract every tag declared in a playbook YAML file.

We walk the parsed YAML recursively and collect anything that appears under a
`tags:` key, whether it's a string or a list. We skip the built-in pseudo-tags
`always` and `never` since users can't usefully select/deselect them in the UI.
This only inspects the named playbook file — we don't follow `import_playbook`,
`roles:`, or `include_tasks` (yet). Tags coming from imported roles will not
show up; users can still type them in the custom-tags field.
"""
from __future__ import annotations

from pathlib import Path

import yaml


PSEUDO_TAGS = {"always", "never", "untagged"}


def _walk(node, into: set[str]) -> None:
    if isinstance(node, dict):
        raw = node.get("tags")
        if isinstance(raw, str):
            into.add(raw.strip())
        elif isinstance(raw, list):
            for x in raw:
                if isinstance(x, str):
                    into.add(x.strip())
        for v in node.values():
            _walk(v, into)
    elif isinstance(node, list):
        for x in node:
            _walk(x, into)


def collect(playbook_path: Path) -> list[str]:
    try:
        with playbook_path.open("r", encoding="utf-8") as fh:
            doc = yaml.safe_load(fh)
    except (yaml.YAMLError, OSError, UnicodeDecodeError):
        return []
    tags: set[str] = set()
    _walk(doc, tags)
    tags = {t for t in tags if t and t not in PSEUDO_TAGS}
    return sorted(tags)
