"""Tests for the rule-based playbook checker (the symbolic verifier)."""
from __future__ import annotations

from app.core import playbook_rules as pr


def _has(findings, substr):
    return any(substr.lower() in f["message"].lower() for f in findings)


def test_clean_playbook_has_no_findings():
    text = (
        "- hosts: all\n"
        "  become: true\n"
        "  vars:\n"
        "    pkg: nginx\n"
        "  tasks:\n"
        "    - name: install\n"
        "      ansible.builtin.apt:\n"
        "        name: \"{{ pkg }}\"\n"
        "        state: present\n"
    )
    assert pr.check_text(text) == []


def test_vars_as_list_is_error():
    text = "- hosts: all\n  vars:\n    - a: 1\n  tasks:\n    - name: t\n      ansible.builtin.ping:\n"
    f = pr.check_text(text)
    assert _has(f, "vars' must be a mapping")
    assert any(x["severity"] == "error" for x in f)


def test_task_without_module_is_error():
    text = "- hosts: all\n  tasks:\n    - name: nothing\n      when: true\n"
    assert _has(pr.check_text(text), "no module")


def test_missing_hosts_is_error():
    assert _has(pr.check_text("- tasks: []\n"), "missing 'hosts'")


def test_listen_in_tasks_flagged():
    text = ("- hosts: all\n  tasks:\n    - name: restart\n"
            "      ansible.builtin.service: {name: ssh, state: restarted}\n"
            "      listen: Restart SSH\n")
    assert _has(pr.check_text(text), "listen")


def test_lockout_when_passwords_disabled_before_key():
    text = ("- hosts: all\n  tasks:\n    - name: disable passwords\n"
            "      ansible.builtin.lineinfile:\n"
            "        path: /etc/ssh/sshd_config\n"
            "        line: \"PasswordAuthentication no\"\n")
    assert _has(pr.check_text(text), "lock")


def test_no_lockout_when_key_added_first():
    text = ("- hosts: all\n  tasks:\n    - name: add key\n"
            "      ansible.posix.authorized_key: {user: deploy, key: k}\n"
            "    - name: disable passwords\n"
            "      ansible.builtin.lineinfile:\n"
            "        path: /etc/ssh/sshd_config\n"
            "        line: \"PasswordAuthentication no\"\n")
    assert not _has(pr.check_text(text), "lock")


def test_destructive_user_absent():
    text = "- hosts: all\n  tasks:\n    - name: del\n      ansible.builtin.user: {name: admin, state: absent}\n"
    assert _has(pr.check_text(text), "destructive")


def test_contradictory_user_state():
    text = ("- hosts: all\n  tasks:\n"
            "    - name: lock\n      ansible.builtin.user: {name: admin, shell: /usr/sbin/nologin}\n"
            "    - name: del\n      ansible.builtin.user: {name: admin, state: absent}\n")
    assert _has(pr.check_text(text), "contradictory")


def test_invalid_yaml_reported():
    assert _has(pr.check_text("- hosts: [unclosed\n"), "invalid yaml")


def test_non_playbook_mapping_ignored():
    # A vars-file style mapping is not a list of plays → no findings.
    assert pr.check_text("key: value\nother: 1\n") == []


def test_check_reply_extracts_yaml_blocks_and_dedupes():
    reply = ("Here you go:\n```yaml\n- hosts: all\n  vars:\n    - a: 1\n"
             "  tasks:\n    - name: t\n      ansible.builtin.ping:\n```\nDone.")
    f = pr.check_reply(reply)
    assert _has(f, "vars' must be a mapping")


def test_full_buggy_playbook_from_the_wild():
    """The exact shape a small model produced for 'harden ubuntu' — every issue caught."""
    text = (
        "- name: Harden\n"
        "  hosts: all\n"
        "  become: true\n"
        "  vars:\n"
        "    - admin_user: admin\n"          # vars as list
        "  tasks:\n"
        "    - name: lock admin\n"
        "      ansible.builtin.user: {name: admin, shell: /usr/sbin/nologin}\n"
        "    - name: delete admin\n"
        "      ansible.builtin.user: {name: admin, state: absent}\n"   # destructive + contradiction
        "    - name: disable passwords\n"
        "      ansible.builtin.lineinfile: {path: /etc/ssh/sshd_config, line: 'PasswordAuthentication no'}\n"  # lockout
        "    - name: restart\n"
        "      ansible.builtin.systemd: {name: ssh, state: restarted}\n"
        "      listen: Restart SSH\n"          # listen in tasks
    )
    f = pr.check_text(text)
    assert _has(f, "vars' must be a mapping")
    assert _has(f, "destructive")
    assert _has(f, "contradictory")
    assert _has(f, "lock")
    assert _has(f, "listen")


def test_lockout_detected_in_pre_tasks():
    """Regression: rules must scan pre_tasks, not just tasks (AI often hardens there)."""
    text = (
        "- hosts: all\n"
        "  pre_tasks:\n"
        "    - name: disable passwords\n"
        "      ansible.builtin.lineinfile:\n"
        "        path: /etc/ssh/sshd_config\n"
        "        line: \"PasswordAuthentication no\"\n"
        "  tasks:\n"
        "    - name: ping\n"
        "      ansible.builtin.ping:\n"
    )
    assert _has(pr.check_text(text), "lock")


def test_no_module_detected_in_post_tasks():
    text = (
        "- hosts: all\n"
        "  tasks:\n"
        "    - name: ping\n"
        "      ansible.builtin.ping:\n"
        "  post_tasks:\n"
        "    - name: orphan\n"
        "      when: true\n"
    )
    assert _has(pr.check_text(text), "no module")


def test_bare_task_list_not_flagged_for_missing_hosts():
    # A tasks/*.yml include file is a list of tasks, not plays — must not get
    # "missing hosts" (regression from a real assistant reply).
    text = (
        "- name: Create user\n"
        "  ansible.builtin.user: {name: deploy, shell: /bin/bash, groups: sudo}\n"
        "- name: Ensure home\n"
        "  ansible.builtin.file: {path: /home/deploy, state: directory}\n"
    )
    findings = pr.check_text(text)
    assert not _has(findings, "missing 'hosts'")


def test_task_list_still_catches_real_issues():
    # Destructive/lockout rules must still run inside a task list.
    text = (
        "- name: del\n"
        "  ansible.builtin.user: {name: admin, state: absent}\n"
        "- name: orphan\n"
        "  when: true\n"
    )
    findings = pr.check_text(text)
    assert _has(findings, "destructive")
    assert _has(findings, "no module")
    assert not _has(findings, "missing 'hosts'")


def test_playbook_with_hosts_still_requires_hosts_on_other_plays():
    # A real play missing hosts is still flagged (we didn't disable the check).
    text = "- hosts: all\n  tasks: []\n- tasks: []\n"
    assert _has(pr.check_text(text), "missing 'hosts'")


def test_ufw_default_deny_without_ssh_allow_is_lockout():
    text = (
        "- hosts: all\n"
        "  tasks:\n"
        "    - name: deny incoming\n"
        "      community.general.ufw: {default: deny, direction: incoming}\n"
        "    - name: enable\n"
        "      community.general.ufw: {state: enabled}\n"
    )
    assert _has(pr.check_text(text), "lock yourself out")


def test_ufw_default_deny_with_ssh_allow_is_safe():
    # The user's playbook DID allow SSH from a specific IP → no false positive.
    text = (
        "- hosts: all\n"
        "  tasks:\n"
        "    - name: allow ssh from ip\n"
        "      community.general.ufw: {rule: allow, proto: tcp, port: 22, src: 10.0.0.5}\n"
        "    - name: deny incoming\n"
        "      community.general.ufw: {default: deny, direction: in}\n"
    )
    assert not _has(pr.check_text(text), "lock yourself out")


def test_ufw_allow_by_app_name_counts_as_ssh():
    text = (
        "- hosts: all\n"
        "  tasks:\n"
        "    - name: allow openssh\n"
        "      community.general.ufw: {rule: allow, name: OpenSSH}\n"
        "    - name: deny incoming\n"
        "      community.general.ufw: {default: deny, direction: incoming}\n"
    )
    assert not _has(pr.check_text(text), "lock yourself out")


def test_role_tasks_are_checked_when_project_root_given(tmp_path):
    """A play with `roles:` must have the role's tasks scanned (lockout/destructive
    ops usually live in roles, not the playbook itself)."""
    (tmp_path / "roles" / "harden" / "tasks").mkdir(parents=True)
    (tmp_path / "roles" / "harden" / "tasks" / "main.yml").write_text(
        "---\n"
        "- name: disable passwords\n"
        "  ansible.builtin.lineinfile:\n"
        "    path: /etc/ssh/sshd_config\n"
        "    line: \"PasswordAuthentication no\"\n"
    )
    play = "- hosts: all\n  roles:\n    - harden\n"
    # Without the project root, the role is invisible → no lockout finding.
    assert not _has(pr.check_text(play), "lock")
    # With it, the role's tasks are expanded and the lockout is caught.
    assert _has(pr.check_text(play, project_root=tmp_path), "lock")


def test_role_dict_form_and_missing_role_are_safe(tmp_path):
    (tmp_path / "roles" / "common" / "tasks").mkdir(parents=True)
    (tmp_path / "roles" / "common" / "tasks" / "main.yml").write_text(
        "---\n- name: del user\n  ansible.builtin.user: {name: admin, state: absent}\n")
    play = "- hosts: all\n  roles:\n    - role: common\n    - role: does_not_exist\n"
    findings = pr.check_text(play, project_root=tmp_path)
    assert _has(findings, "destructive")  # from common role
    # Missing role must not crash or add noise.


def test_key_in_pre_tasks_protects_disable_in_tasks():
    """authorized_key in pre_tasks runs before tasks, so no lockout warning."""
    text = (
        "- hosts: all\n"
        "  pre_tasks:\n"
        "    - name: add key\n"
        "      ansible.posix.authorized_key: {user: deploy, key: k}\n"
        "  tasks:\n"
        "    - name: disable passwords\n"
        "      ansible.builtin.lineinfile:\n"
        "        path: /etc/ssh/sshd_config\n"
        "        line: \"PasswordAuthentication no\"\n"
    )
    assert not _has(pr.check_text(text), "lock")
