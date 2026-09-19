-- 004: complete the seeded proj_default / agent_default rows with their real
-- default field values (docs/design/01-w2-interfaces.md §2 §4, issue #8 #9).
--
-- Branch A's 002 migration seeds these two ids as minimal placeholders so the main
-- session (created at daemon startup, before C's modules necessarily exist) has
-- something to reference; §4 assigns this migration the job of filling in their
-- real content. Uses INSERT ... ON CONFLICT(id) DO UPDATE rather than assuming the
-- row already exists, so this migration is correct regardless of merge order (in
-- particular: in this branch's own isolated worktree/tests, where 002 has not been
-- applied, the INSERT branch fires and creates both rows outright).
--
-- `projects.path` is deliberately NOT set to the real user home directory here — a
-- static SQL migration file can't know a per-machine path. That correction happens
-- at runtime, once, via ProjectService.ensure_default_project() (see
-- projects/service.py), called from __main__.py at daemon startup. This migration
-- only touches fields that don't depend on the running machine.

INSERT INTO projects (id, path, name, settings_json, created_at, updated_at)
VALUES (
    'proj_default',
    '__pending_home__',
    'Default',
    '{}',
    strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
    strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
)
ON CONFLICT(id) DO UPDATE SET
    name = 'Default';

INSERT INTO agents (
    id, project_id, name, persona, tone, principles,
    tool_allowlist_json, skills_json, model_pref_json, created_at, updated_at
)
VALUES (
    'agent_default',
    NULL,
    'Default Agent',
    NULL,
    NULL,
    NULL,
    '[]',
    '[]',
    '{}',
    strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
    strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
)
ON CONFLICT(id) DO UPDATE SET
    project_id = NULL,
    name = 'Default Agent',
    -- 白名单为空 = 全部工具经闸 (01-w2-interfaces.md §2), not "no tools allowed" —
    -- see agents/policy.py::is_tool_allowlist_subset for the semantics this backs.
    tool_allowlist_json = '[]',
    skills_json = '[]',
    model_pref_json = '{}';
