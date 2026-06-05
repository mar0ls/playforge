"""Tests for multi-file extraction from assistant replies (the `# file:` convention)."""
from __future__ import annotations

from app.core.ai import extract_files


def test_no_file_blocks_returns_empty():
    assert extract_files("Just a plain answer, no code.") == []
    # A normal code block without a `# file:` header is not a saveable file.
    assert extract_files("```yaml\n- hosts: all\n```") == []


def test_single_file_block():
    reply = "Here:\n```yaml\n# file: playbooks/site.yml\n- hosts: all\n  tasks: []\n```"
    files = extract_files(reply)
    assert len(files) == 1
    assert files[0]["path"] == "playbooks/site.yml"
    assert files[0]["lang"] == "yaml"
    assert "- hosts: all" in files[0]["content"]
    assert "# file:" not in files[0]["content"]  # header stripped from body


def test_multiple_files_playbook_template_script():
    reply = (
        "```yaml\n# file: playbooks/web.yml\n- hosts: web\n```\n"
        "```jinja\n# file: templates/nginx.conf.j2\nserver { listen {{ port }}; }\n```\n"
        "```bash\n# file: scripts/setup.sh\n#!/usr/bin/env bash\nset -e\n```\n"
    )
    files = extract_files(reply)
    paths = [f["path"] for f in files]
    assert paths == ["playbooks/web.yml", "templates/nginx.conf.j2", "scripts/setup.sh"]
    assert files[1]["content"].strip() == "server { listen {{ port }}; }"


def test_unsafe_paths_are_dropped():
    reply = (
        "```yaml\n# file: /etc/passwd\nx\n```\n"
        "```yaml\n# file: ../escape.yml\ny\n```\n"
        "```yaml\n# file: ~/secret\nz\n```\n"
        "```yaml\n# file: ok/inside.yml\ngood\n```\n"
    )
    files = extract_files(reply)
    assert [f["path"] for f in files] == ["ok/inside.yml"]


def test_dotslash_prefix_normalised_and_dedup():
    reply = (
        "```yaml\n# file: ./playbooks/a.yml\nfirst\n```\n"
        "```yaml\n# file: playbooks/a.yml\nsecond\n```\n"  # same path after normalise → dropped
    )
    files = extract_files(reply)
    assert len(files) == 1
    assert files[0]["path"] == "playbooks/a.yml"
    assert "first" in files[0]["content"]


def test_lang_optional():
    reply = "```\n# file: notes.txt\nhello\n```"
    files = extract_files(reply)
    assert files[0]["path"] == "notes.txt"
    assert files[0]["lang"] == ""
