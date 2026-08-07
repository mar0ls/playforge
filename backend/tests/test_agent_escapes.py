"""normalize_escapes: literal \\n from weak models → real newlines, conservatively."""
from __future__ import annotations

import pytest

pytest.importorskip("yaml")

from app.core.agent_tools import normalize_escapes


def test_single_line_playbook_becomes_real_yaml():
    src = '---\\n- hosts: all\\n  tasks: []'
    out, changed = normalize_escapes(src)

    assert changed is True
    assert out == "---\n- hosts: all\n  tasks: []"
    import yaml
    assert yaml.safe_load(out)[0]["hosts"] == "all"


def test_content_with_real_newlines_is_untouched():
    """A playbook that legitimately contains \\n inside a string must survive."""
    src = '---\n- hosts: all\n  tasks:\n    - debug: msg="line1\\nline2"\n'
    out, changed = normalize_escapes(src)

    assert changed is False
    assert out == src


def test_content_without_escapes_is_untouched():
    src = "---\n- hosts: all\n"
    assert normalize_escapes(src) == (src, False)


def test_empty_content():
    assert normalize_escapes("") == ("", False)


def test_crlf_and_tabs_and_quotes():
    src = '{\\r\\n\\t"a": \\"b\\"\\r\\n}'
    out, changed = normalize_escapes(src)

    assert changed is True
    assert out == '{\n\t"a": "b"\n}'


def test_non_ascii_is_not_corrupted():
    """Why this uses explicit replacement, not codecs unicode_escape."""
    src = '---\\n# zażółć gęślą jaźń\\n- hosts: all'
    out, changed = normalize_escapes(src)

    assert changed is True
    assert "zażółć gęślą jaźń" in out
    assert out.splitlines()[1] == "# zażółć gęślą jaźń"
