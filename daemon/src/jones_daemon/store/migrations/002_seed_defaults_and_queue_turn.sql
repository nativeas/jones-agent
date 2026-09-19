-- 002_seed_defaults_and_queue_turn.sql (owner: A, w2/10-sessions-workers, issue #10)
--
-- Two independent things, both scoped to A's ownership of sessions/turns/queue:
--
-- 1. Placeholder Project/Agent rows (docs/design/01-w2-interfaces.md §2 "主会话"):
--    Session.create requires a valid project_id/agent_id FK. C (#8/#9) hasn't landed
--    Project/Agent yet, so A seeds the minimal placeholder rows the main session is
--    created against. IDs are fixed contract surface — 'proj_default' / 'agent_default'
--    — and MUST NOT change; C's 004 migration is expected to UPDATE these same rows
--    with real fields (real path = user home dir, real persona/tool_allowlist/etc),
--    never re-INSERT them. `projects.path` here is an obvious non-filesystem sentinel,
--    not yet "the user's home directory" — that real semantics is C's FR02 work.
--
-- 2. `queue_items.turn_id`: 00-foundation.md §5 models a Turn as "one user input"
--    (1 Turn -> 1 Run) but queue_items has no turn_id column, which makes "the queued
--    input already has a Turn row, sitting in 'queued' status until its turn to run"
--    unrepresentable. Every session.send() call creates its Turn (and the user
--    Message) immediately, visible in turn.messages right away, regardless of whether
--    it starts running immediately or waits behind others in queue_items — a single
--    consistent model instead of two different shapes for "ran immediately" vs
--    "was queued". This is an additive schema change under A's own ownership
--    (queue/turn is squarely #10), called out here per the "改接口先改文档" rule; see
--    docs/design/01-w2-interfaces.md §2 for the corresponding contract note.
--
-- 3. `permission_decisions.decided_by` was declared `NOT NULL` in 001_init.sql, but
--    00-foundation.md §5's own column description — "decided_by(rule/model/user)" —
--    is only meaningful once a decision has actually been made; a freshly-created
--    request (`decision='pending'`, ACP's `session/request_permission` still
--    in flight, nobody has decided anything yet) has no honest non-NULL value to put
--    there. This is a genuine bug in 001, not a design choice to route around with a
--    sentinel string in application code (DEV.md 工程原则 #2: 不打补丁) — confirmed by
--    actually running the INSERT `sessions/queries.py::insert_permission_decision`
--    needs to make and watching SQLite raise `NOT NULL constraint failed`. Never
--    modify an already-applied migration in place (001 may already be applied to a
--    running dev DB) — fixed here, in A's own 002, via SQLite's standard
--    rebuild-the-table ALTER pattern. The table is empty at this migration step in
--    every real deployment (permission_decisions didn't exist as a concept before
--    #10), so the copy step below is for correctness/symmetry with the general
--    pattern, not because there's data expected to be present.

ALTER TABLE queue_items ADD COLUMN turn_id TEXT REFERENCES turns(id);

CREATE TABLE permission_decisions_new (
    id TEXT PRIMARY KEY,
    step_id TEXT REFERENCES steps(id),
    gate TEXT NOT NULL,
    risk TEXT NOT NULL,
    decision TEXT NOT NULL,
    decided_by TEXT,
    request_json TEXT NOT NULL,
    decided_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
INSERT INTO permission_decisions_new
    SELECT id, step_id, gate, risk, decision, decided_by, request_json, decided_at, created_at, updated_at
    FROM permission_decisions;
DROP TABLE permission_decisions;
ALTER TABLE permission_decisions_new RENAME TO permission_decisions;
CREATE INDEX IF NOT EXISTS ix_permission_decisions_step_id ON permission_decisions(step_id);

INSERT OR IGNORE INTO projects (id, path, name, settings_json, created_at, updated_at)
VALUES (
    'proj_default',
    '__jones_default_project__',
    'Default',
    '{}',
    strftime('%Y-%m-%dT%H:%M:%SZ', 'now'),
    strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
);

INSERT OR IGNORE INTO agents (
    id, project_id, name, persona, tone, principles,
    tool_allowlist_json, skills_json, model_pref_json, created_at, updated_at
)
VALUES (
    'agent_default',
    NULL,
    'Default',
    NULL,
    NULL,
    NULL,
    '[]',
    '[]',
    '{}',
    strftime('%Y-%m-%dT%H:%M:%SZ', 'now'),
    strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
);
