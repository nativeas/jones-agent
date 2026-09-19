-- 006: add `crons.name` (owner: L, w5/20-cron, issue #20).
--
-- 00-foundation.md §5's `crons` column list (project_id, agent_id, expr, prompt,
-- mode, enabled, last_run_at, next_run_at, fail_count) has no human-readable name,
-- but 04-w5-interfaces.md §2 requires one: "SessionService.create(..., title=cron
-- 名)" — the child session's title, and the same text this feature uses in the
-- system messages it posts back to the main session ("Cron “<name>” 已完成/失败/
-- 已自动停用..."). Without a stored name there is nothing honest to put in either
-- place (falling back to the raw `expr` or `id` reads as a bug, not a feature).
--
-- Additive column, not a repurposed one (DEV.md 工程原则 #2: 改接口先改文档，同一
-- PR 内 — see this branch's report for the corresponding note). `NOT NULL DEFAULT
-- ''` rather than nullable: every write path this branch owns (`cron.upsert`)
-- always supplies a name, so a NULL here would only ever mean "written by code
-- that doesn't know about this column yet" — not a real, honest "no name" state.

ALTER TABLE crons ADD COLUMN name TEXT NOT NULL DEFAULT '';
