# Threat model (pytm)

`model.py` is Playforge's trust-boundary model as [pytm](https://github.com/izar/pytm)
code: boundaries, actors, processes, datastores, and the dataflows between them, each
carrying the `controls` flags that are actually true of the current code and a `note`
that cites the source (and, where one exists, the regression test) backing that claim.

This is a companion to [`../../SECURITY.md`](../../SECURITY.md), not a replacement for
it. SECURITY.md is the prose explanation of why each boundary is drawn where it is;
this directory is the same boundaries as versioned, diffable code, so a change to a
trust assumption — a dataflow losing its authentication flag, a sandbox being removed —
shows up as a line in a diff instead of only living in someone's head. When the two
disagree, SECURITY.md's prose and the actual code are authoritative; fix this model to
match.

## Setup

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Usage

```sh
# Data-flow diagram (needs graphviz's `dot`: brew install graphviz / apt install graphviz)
.venv/bin/python3 model.py --dfd | dot -Tsvg -o dfd.svg

# Sequence diagram (PlantUML source; paste into plantuml.com/plantuml or a local renderer)
.venv/bin/python3 model.py --seq > sequence.puml

# Machine-readable dump, then a human-readable summary rendered from it
.venv/bin/python3 model.py --json model.json
.venv/bin/python3 summarize.py model.json > summary.md

# Every threat rule pytm's bundled library knows about (see caveat below)
.venv/bin/python3 model.py --list
```

None of these generated files (`dfd.svg`, `sequence.puml`, `model.json`, `summary.md`)
are committed — they're cheap to regenerate from `model.py`, and a committed copy would
just go stale. The CI `threat-model` job regenerates them on every push/PR and uploads
them as a build artifact, so a reviewer can open the diagram and summary from the run
without a local pytm install.

## Why `summarize.py` and not pytm's own `--report`

pytm 1.4.0's `--report <template.md>` feature is broken on this pytm/Python
combination: its `{value:repeat:template}` custom format spec needs a nested field
(e.g. `{item.name}`) inside the format spec, but Python's standard `string.Formatter`
tries to resolve that nested field itself, against the *outer* scope, before pytm's
code ever runs — so it fails with `KeyError: 'item'` on the simplest possible repeat
template. That's a pytm bug, not something to route around inside `model.py`.
`summarize.py` reads pytm's `--json` output instead (a stable, documented interface)
and formats it with plain Python string formatting.

## A pytm quirk that shapes how `model.py` is written

`Dataflow.protocol`, `.srcPort`/`.dstPort`, `.controls.isEncrypted` and
`.controls.authenticatesDestination` are **not** read back from what you pass into the
`Dataflow()` constructor. During `tm.check()`, pytm unconditionally overwrites them
from the flow's *endpoint elements*: protocol/port/isEncrypted come from the sink,
authenticatesDestination comes from the source. So those five fields are set on the
`Server`/`Datastore`/`Actor` elements in `model.py`, not on the `Dataflow(...)` calls —
setting them on a Dataflow is silently discarded. Every other `Controls` flag
(`implementsPOLP`, `validatesInput`, `sanitizesInput`, `authorizesSource`,
`handlesResourceConsumption`, `providesIntegrity`, ...) is not touched by that step and
does belong on the `Dataflow`.

One consequence: because those five fields live on the *element*, not the flow, the
Playforge app element — which serves HTTPS to the operator but is the *client* end of
the SSH run, the AI-provider call and the git push/pull — can't represent that nuance
per-flow. The per-flow `note` text is written to stay correct regardless; treat it, not
the protocol/port fields, as authoritative for those flows.

## On the built-in threat library

pytm ships a bundled catalog of generic, largely web-application-oriented threat rules
(SQL injection, SSRF, XML entity expansion, …), matched automatically against whatever
`controls` flags are *not* set. Most of it is not relevant to a self-hosted Ansible
controller, so `--json`'s `findings` array is mostly noise for this architecture —
`summarize.py`'s output says so inline. The seven dataflow `note`s in `model.py` are
the real, codebase-specific record this model exists to track; the bundled catalog is
left enabled anyway because an occasional genuine hit is still worth a human second
look, and excluding rules one by one would be more speculative effort than reading past
the noise.
