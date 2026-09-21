-- 008: 挂起状态落库 (controller ruling R-N9, round-6, 2026-09-20;
-- docs/design/04-w5-interfaces.md §4.3/§4.4, PRD 9.3).
--
-- R-N4 (round-5) already made "终止不清空 Session 队列；队列中的后续指令挂起"
-- real, but only as ephemeral state: `_advance_queue`'s `queue.changed`
-- broadcast (`suspended`/`reason`) and the renderer's own in-memory
-- `queueSuspendedReason` are both gone the moment the daemon restarts or the
-- user switches away from the session's pane and back (the broadcast already
-- fired once and is never replayed; a fresh `bindSession()` had nothing to
-- reconstruct it from). Round-6 review: "挂起状态必须可查询、可重建，不能只
-- 活在 renderer 内存里."
--
-- Nullable, same reasoning as `runs.terminated_kind` (001_init.sql): NULL
-- means "not suspended" (the vast majority of sessions, always, and every
-- session ever created before this migration ships — DEV.md 工程原则 #4 诚实
-- 失败, no fabricated backfill), a non-NULL value is one of the three outer
-- termination kinds ("user"|"error"|"budget") `_terminate_run` already
-- classifies.

ALTER TABLE sessions ADD COLUMN queue_suspended_reason TEXT;
