#!/usr/bin/env bash
# Set up the "failed run -> Fix with agent" demo, then point you at the UI to
# record it. Runs against localhost (no remote target needed). Idempotent-ish:
# creates a fresh project each time.
#
#   BASE_URL=http://127.0.0.1:8765 scripts/demo.sh
#
# What it does:
#   1. creates a project with a playbook that uses a typo'd module
#      (ansible.builtin.coppy) so the run fails on localhost
#   2. runs it -> fails
#   3. prints the run URL: open it, click "Fix with agent", watch the agent read
#      the failure, fix the playbook (coppy -> copy), and preview it, then "Re-run"
#
# To capture a GIF: record your browser while doing step 3 (any screen recorder),
# or record a terminal walkthrough with asciinema/vhs.
set -euo pipefail

BASE_URL="${BASE_URL:-http://127.0.0.1:8765}"

req() { curl -fsS -m 60 "$@"; }
jget() { python3 -c "import sys,json;print(json.load(sys.stdin)[sys.argv[1]])" "$1"; }

echo "→ creating demo project…"
PID=$(req -X POST "$BASE_URL/api/projects" -H "Content-Type: application/json" \
        -d '{"name":"playforge-demo"}' | jget id)

req -X PUT "$BASE_URL/api/projects/$PID/file" -H "Content-Type: application/json" \
  -d '{"path":"ansible.cfg","content":"[defaults]\ninventory = hosts\n"}' >/dev/null

req -X PUT "$BASE_URL/api/projects/$PID/file" -H "Content-Type: application/json" \
  -d '{"path":"hosts","content":"[local]\nlocalhost ansible_connection=local\n"}' >/dev/null

# Typo'd module name -> the run fails; the agent (and the validation layer) can fix it.
req -X PUT "$BASE_URL/api/projects/$PID/file" -H "Content-Type: application/json" \
  -d '{"path":"playbooks/site.yml","content":"---\n- name: Write a marker file\n  hosts: local\n  gather_facts: false\n  tasks:\n    - name: write file\n      ansible.builtin.coppy:\n        content: hello\n        dest: /tmp/playforge_demo.txt\n"}' >/dev/null

echo "→ running it (expected to fail on the typo'd module)…"
RID=$(req -X POST "$BASE_URL/api/runs" -H "Content-Type: application/json" \
        -d "{\"project_id\":\"$PID\",\"playbook\":\"playbooks/site.yml\",\"inventory\":\"hosts\"}" | jget run_id)

cat <<EOF

Demo is set up.

  Run page:  $BASE_URL/projects/$PID/runs/$RID

Open it and click "Fix with agent" — the agent reads the run, fixes the playbook
(ansible.builtin.coppy -> ansible.builtin.copy), and previews it. Then "Re-run".

Record your browser there for the GIF. Clean up afterwards:
  curl -X DELETE $BASE_URL/api/projects/$PID
EOF
