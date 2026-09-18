-- Schema v1 (docs/design/00-foundation.md §5, PRD §7). Table names = domain object
-- names, snake_case plural. Every domain table has id TEXT PRIMARY KEY (ULID),
-- created_at, updated_at (ISO-8601 UTC text).

CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    path TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    settings_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agents (
    id TEXT PRIMARY KEY,
    project_id TEXT REFERENCES projects(id),
    name TEXT NOT NULL,
    persona TEXT,
    tone TEXT,
    principles TEXT,
    tool_allowlist_json TEXT NOT NULL DEFAULT '[]',
    skills_json TEXT NOT NULL DEFAULT '[]',
    model_pref_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_agents_project_id ON agents(project_id);

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id),
    agent_id TEXT NOT NULL REFERENCES agents(id),
    parent_id TEXT REFERENCES sessions(id),
    is_main INTEGER NOT NULL DEFAULT 0,
    mode TEXT NOT NULL,
    title TEXT,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_sessions_project_id ON sessions(project_id);
CREATE INDEX IF NOT EXISTS ix_sessions_parent_id ON sessions(parent_id);
-- Exactly one main session may exist at a time — globally, not per project
-- (design §5: PRD 要求"主会话唯一、常驻"，未按 project 切分；裁定为全局唯一，
-- 索引不带 project_id 前缀，是最终决定，不是待评审的临时选择).
CREATE UNIQUE INDEX IF NOT EXISTS ux_sessions_is_main ON sessions(is_main) WHERE is_main = 1;

CREATE TABLE IF NOT EXISTS turns (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    user_message_id TEXT,
    run_id TEXT,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_turns_session_id ON turns(session_id);

CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    turn_id TEXT REFERENCES turns(id),
    role TEXT NOT NULL,
    content_json TEXT NOT NULL,
    seq INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_messages_session_id_seq ON messages(session_id, seq);
CREATE INDEX IF NOT EXISTS ix_messages_turn_id ON messages(turn_id);

CREATE TABLE IF NOT EXISTS goals (
    id TEXT PRIMARY KEY,
    session_id TEXT REFERENCES sessions(id),
    project_id TEXT REFERENCES projects(id),
    title TEXT NOT NULL,
    budget_tokens INTEGER,
    spent_tokens INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS crons (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id),
    agent_id TEXT NOT NULL REFERENCES agents(id),
    expr TEXT NOT NULL,
    prompt TEXT NOT NULL,
    mode TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    last_run_at TEXT,
    next_run_at TEXT,
    fail_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_crons_project_id ON crons(project_id);

CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    goal_id TEXT REFERENCES goals(id),
    cron_id TEXT REFERENCES crons(id),
    source TEXT NOT NULL,
    status TEXT NOT NULL,
    title TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_tasks_session_id ON tasks(session_id);

CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    task_id TEXT REFERENCES tasks(id),
    turn_id TEXT REFERENCES turns(id),
    session_id TEXT NOT NULL REFERENCES sessions(id),
    status TEXT NOT NULL,
    started_at TEXT,
    ended_at TEXT,
    terminated_kind TEXT,
    terminated_reason TEXT,
    prompt_snapshot_ref TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_runs_session_id ON runs(session_id);
CREATE INDEX IF NOT EXISTS ix_runs_task_id ON runs(task_id);

CREATE TABLE IF NOT EXISTS steps (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id),
    seq INTEGER NOT NULL,
    tool TEXT NOT NULL,
    args_json TEXT NOT NULL DEFAULT '{}',
    result_summary TEXT,
    payload_ref TEXT,
    duration_ms INTEGER,
    permission_id TEXT,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_steps_run_id_seq ON steps(run_id, seq);

CREATE TABLE IF NOT EXISTS permission_decisions (
    id TEXT PRIMARY KEY,
    step_id TEXT REFERENCES steps(id),
    gate TEXT NOT NULL,
    risk TEXT NOT NULL,
    decision TEXT NOT NULL,
    decided_by TEXT NOT NULL,
    request_json TEXT NOT NULL,
    decided_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_permission_decisions_step_id ON permission_decisions(step_id);

CREATE TABLE IF NOT EXISTS queue_items (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    text TEXT NOT NULL,
    attachments_json TEXT NOT NULL DEFAULT '[]',
    position INTEGER NOT NULL,
    state TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_queue_items_session_id_position ON queue_items(session_id, position);

CREATE TABLE IF NOT EXISTS providers (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    has_key INTEGER NOT NULL DEFAULT 0,
    key_hint TEXT,
    default_model TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
