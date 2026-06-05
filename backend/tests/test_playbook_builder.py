"""Unit tests for the structured playbook builder (pure YAML generation)."""
from __future__ import annotations

import pytest
import yaml

from app.core.playbook_builder import BuilderError, build_playbook


def _load_single_play(spec: dict) -> dict:
    docs = yaml.safe_load(build_playbook(spec))
    assert isinstance(docs, list) and len(docs) == 1
    return docs[0]


def test_minimal_spec_defaults_name_and_hosts():
    play = _load_single_play({"tasks": []})
    assert play["name"] == "Playbook"
    assert play["hosts"] == "all"
    assert play["tasks"] == []


def test_become_and_gather_facts_emission():
    play = _load_single_play({"hosts": "web", "become": True, "gather_facts": False, "tasks": []})
    assert play["become"] is True
    assert play["gather_facts"] is False


def test_gather_facts_true_is_not_emitted():
    # Only an explicit `false` is written; the Ansible default (true) stays implicit.
    play = _load_single_play({"gather_facts": True, "tasks": []})
    assert "gather_facts" not in play


def test_task_args_yaml_parsed_into_mapping():
    play = _load_single_play({
        "tasks": [{
            "name": "Install nginx",
            "module": "ansible.builtin.apt",
            "args_yaml": "name: nginx\nstate: present",
        }],
    })
    task = play["tasks"][0]
    assert task["name"] == "Install nginx"
    assert task["ansible.builtin.apt"] == {"name": "nginx", "state": "present"}


def test_tags_string_is_split_into_list():
    play = _load_single_play({
        "tasks": [{"name": "t", "module": "ping", "tags": "a, b ,c"}],
    })
    assert play["tasks"][0]["tags"] == ["a", "b", "c"]


def test_when_clause_passed_through():
    play = _load_single_play({
        "tasks": [{"name": "t", "module": "ping", "when": "x == 1"}],
    })
    assert play["tasks"][0]["when"] == "x == 1"


def test_empty_args_yaml_yields_empty_mapping():
    play = _load_single_play({"tasks": [{"name": "t", "module": "ping", "args_yaml": "  "}]})
    assert play["tasks"][0]["ping"] == {}


@pytest.mark.parametrize("bad_task, fragment", [
    ({"module": "ping"}, "name is required"),
    ({"name": "t"}, "module is required"),
    ({"name": "t", "module": "ping", "args_yaml": "name: [unclosed"}, "YAML parse error"),
    ({"name": "t", "module": "ping", "args_yaml": "just a string"}, "must be a YAML mapping"),
])
def test_invalid_task_raises_builder_error(bad_task, fragment):
    with pytest.raises(BuilderError) as exc:
        build_playbook({"tasks": [bad_task]})
    assert fragment in str(exc.value)


def test_non_dict_task_rejected():
    with pytest.raises(BuilderError):
        build_playbook({"tasks": ["not a dict"]})


# --- advanced playbook features ---------------------------------------------

def test_play_level_vars_serial_strategy_become_user():
    play = _load_single_play({
        "name": "Adv", "hosts": "web", "become": True, "become_user": "deploy",
        "serial": "2", "strategy": "free", "vars_yaml": "app_port: 8080\nenv: prod",
        "tasks": [{"name": "t", "module": "ansible.builtin.ping"}],
    })
    assert play["become_user"] == "deploy"
    assert play["serial"] == 2          # coerced to int
    assert play["strategy"] == "free"
    assert play["vars"] == {"app_port": 8080, "env": "prod"}


def test_serial_percentage_kept_as_string():
    play = _load_single_play({"serial": "30%", "tasks": [{"name": "t", "module": "ping"}]})
    assert play["serial"] == "30%"


def test_task_become_loop_register_notify():
    play = _load_single_play({
        "tasks": [{
            "name": "Install", "module": "ansible.builtin.apt",
            "args_yaml": "name: \"{{ item }}\"\nstate: present",
            "become": True, "become_user": "root",
            "loop": "nginx, git, curl",
            "register": "apt_result",
            "notify": "restart nginx",
        }],
    })
    task = play["tasks"][0]
    assert task["become"] is True
    assert task["become_user"] == "root"
    assert task["loop"] == ["nginx", "git", "curl"]
    assert task["register"] == "apt_result"
    assert task["notify"] == ["restart nginx"]


def test_loop_jinja_expression_passthrough():
    play = _load_single_play({
        "tasks": [{"name": "t", "module": "ansible.builtin.debug",
                   "args_yaml": "msg: x", "loop": "{{ users }}"}],
    })
    assert play["tasks"][0]["loop"] == "{{ users }}"


def test_handlers_section():
    play = _load_single_play({
        "tasks": [{"name": "edit", "module": "ansible.builtin.copy",
                   "args_yaml": "src: a\ndest: /b", "notify": "restart nginx"}],
        "handlers": [{"name": "restart nginx", "module": "ansible.builtin.service",
                      "args_yaml": "name: nginx\nstate: restarted"}],
    })
    assert "handlers" in play
    assert play["handlers"][0]["name"] == "restart nginx"
    assert play["handlers"][0]["ansible.builtin.service"]["state"] == "restarted"


def test_bad_play_vars_raises():
    with pytest.raises(BuilderError):
        build_playbook({"vars_yaml": "- not a mapping", "tasks": []})
