-- 005: Run 回放 (docs/design/02-w3-interfaces.md §2, issue #12).
--
-- `runs.terminated_kind`/`terminated_reason` (001_init.sql) already record *why* a
-- Run ended; FR06/G07 also requires recording *where* — "终止记录：_terminate_run
-- 写 terminated_kind/reason 与终止时的 step_seq" — so a replay can show which Step
-- was in flight (or the next one that never started) when the Run ended, not just
-- prose. No existing column carries this ("payload_ref" is per-Step, not per-Run),
-- so this is an additive column, not a repurposed one.
--
-- Nullable: every Run terminated before this migration ships has no honest value to
-- backfill (DEV.md 工程原则 #4 诚实失败 — NULL means "not recorded", not "step 0").

ALTER TABLE runs ADD COLUMN terminated_step_seq INTEGER;
