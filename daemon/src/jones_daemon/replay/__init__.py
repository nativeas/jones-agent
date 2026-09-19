"""Run 回放 (FR06, docs/design/02-w3-interfaces.md §2): full-fidelity payload
storage on disk (`store.py`) plus the retention/purge policy that cleans it up
(`retention.py`). SQLite (`runs`/`steps`/`permission_decisions`) stays the sole
source of truth for state (PRD 10.4); everything in this package is the
filesystem-only "attachment" to that state — losing it degrades replay detail,
never correctness (PRD 10.3's `payload_ref` row).
"""
