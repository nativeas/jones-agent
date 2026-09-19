-- 007_default_agent_research_skill.sql (owner: J, w4/15-16-browser-research, issue #16)
--
-- Controller ruling R-J5 (round-2 review, 2026-09-19): the default Agent's Skill
-- set must include Hermes's bundled `research/grounded-citations` skill (PRD FR10
-- "带引用的报告" — see the report's "调研结论" for why this skill, not Jones-written
-- code, already owns the ledger/citation mechanics DEV.md 工程原则 #1 says to reuse).
-- This is a genuine, additive fix, not scope creep: it's the one piece of #16 that
-- was Issue-required but landed with zero Jones-side code (see the report's "没做
-- 什么" #2 for why the branch's own author originally judged it out of reach).
--
-- Why 006 and not touching 002/004: `002_seed_defaults_and_queue_turn.sql` (A,
-- #10) seeds the placeholder `agent_default` row and its own header explicitly
-- says later branches must UPDATE it, never re-INSERT/edit that file in place;
-- `004_projects_agents.sql` (C, #8/#9) is the migration §1 grants C ownership of
-- for filling in the row's real default fields (including `skills_json = '[]'`)
-- and isn't this branch's file to edit either — never modify an already-applied
-- migration file in place (a running dev DB may already have it applied; see
-- 001/002's own precedent for this rule). An additive UPDATE-only migration is
-- the only shape that's safe regardless of merge order with A/C's work, matching
-- 004's own "INSERT ... ON CONFLICT DO UPDATE" precedent for the same row.
--
-- json_insert (not json_set/a literal overwrite) + a NOT EXISTS guard: idempotent
-- (re-running this file, or re-deriving schema_version some other way, must not
-- append the entry twice) and additive (a future branch that's already appended
-- some other default skill to this same array before this migration runs must not
-- have that entry clobbered).

UPDATE agents
SET skills_json = json_insert(skills_json, '$[#]', 'research/grounded-citations'),
    updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
WHERE id = 'agent_default'
  AND NOT EXISTS (
    SELECT 1 FROM json_each(agents.skills_json)
    WHERE json_each.value = 'research/grounded-citations'
  );
