# Golden set for the self-checking layers

Regression cases for the two layers that make the AI output trustworthy: the
symbolic rule engine (`core/playbook_rules`) and the module validator
(`core/ai_validate`). Both are deterministic, so these cases assert exact
behaviour with no model in the loop — the point is to notice when *our* checking
regresses, independently of which LLM is configured.

Run with the rest of the suite; the harness is `tests/test_golden_rules.py`.

## Adding a case

One `.yml` file per case:

```yaml
name: short description
why: what this protects against, and why it matters operationally
content: |
  ---
  - hosts: all
    tasks:
      - name: something
        ansible.builtin.debug:
          msg: hi
expect_rules: [ufw-lockout-no-ssh]     # rules that MUST fire
expect_absent: [ssh-lockout-password-auth]   # rules that must NOT fire (optional)
expect_invalid_modules: [ansible.builtin.ufw]      # optional, needs ansible-doc
expect_uninstalled_modules: []                     # optional, needs ansible-doc
```

`expect_rules` matches on the stable `rule` id from `playbook_rules`, never on
message text — messages get reworded, and a golden set that breaks on rewording
produces noise instead of signal.

Cases with `expect_invalid_modules` / `expect_uninstalled_modules` need
`ansible-doc` and the baked collections, so they only run inside the image and
are skipped in a bare venv.

## Guidance

A good case is one a real user hit, or one a model got wrong during dogfooding.
Prefer a realistic playbook over a minimal synthetic one — the rules run over
whole plays (pre_tasks, roles, handlers), and the ordering-sensitive checks only
mean something with realistic task sequences.
