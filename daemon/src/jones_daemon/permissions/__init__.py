"""Daemon-side halves of FR05's three permission gates (Issue #11,
docs/design/02-w3-interfaces.md §1.1):

- `gate_config`: builds/writes `<HERMES_HOME>/jones_gate.json`, the config the
  rule gate (`kernel/plugin/jones_gate/`, a separate, self-contained package —
  see its own module docstring for why it can't import this one) reads.
- `review`: the review gate's deterministic risk classifier, `classify()`.

Both are plain importable modules, not classes — there's no per-session
state here; `sessions/service.py` calls these as pure functions on the DB
thread (`store.run_in_db_thread`).
"""

from __future__ import annotations
