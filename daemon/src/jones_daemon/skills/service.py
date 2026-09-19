"""Skill discovery for the three tiers PRD 11.4 / 03-w4-interfaces.md §5 names:
project (`<project>/.jones/skills/`), user (`~/.jones/skills/`), builtin
(shipped with the daemon, `jones_daemon/skills/bundled/` — empty in W4, W5
populates it per that section's note). Skill *format* is untouched: a
directory holding `SKILL.md` (YAML frontmatter + instructions), optionally
`references/`/`templates/`/`assets/`/`scripts/` — identical to Hermes's own
convention, so nothing here rewrites or reformats a skill.

## What this module does NOT do: re-implement Hermes's skill loader

`list_skills()` below is a lightweight preview scanner for the daemon's own
`skill.list` RPC (settings UI: "what would load") — it is deliberately not
byte-for-byte identical to Hermes's own scanner (`agent/skill_utils.py` /
`tools/skills_tool.py` in the installed `hermes-agent` checkout,
`/Users/nativeas/.hermes/hermes-agent`, read for this branch): Hermes's real
scan resolves `plugin:skill` namespaces and honors `skills.disabled`/platform
filters. Re-deriving all of that here would be exactly the "重写 Hermes 已有
工具" DEV.md forbids (工程原则 #1) — the actual load-time source of truth is
Hermes itself, once `worker_skill_dirs()` below hands it the right
directories. This scanner only needs to answer "does a SKILL.md exist here
and does its frontmatter parse" for the settings page; edge cases in that
differ-from-Hermes list are called out in this PR's report.

**评审第 1 轮 #5 更正**：上一版这里写着"Hermes 的真实扫描还会用
`skills_guard` 这个内容扫描器隔离项目级 skill"——这句话不对，已核对源码
改正：`agent/skill_utils.py:337 get_external_skills_dirs()` 对 `external_dirs`
只做"路径存在且是目录"校验，`iter_skill_index_files()` 照常索引；
`skills_guard`（`tools/plugin_guard.py`）只在 `hermes skills install` /
`hermes plugins install` 这两条命令式安装路径上被调用（`tools/skills_hub.py`），
**不覆盖 `external_dirs` 的运行时扫描**——也就是说 `worker_skill_dirs()` 把
项目目录写进 `skills.external_dirs` 之后，Hermes 侧对这些项目级 skill 没有
任何内容扫描或信任门。项目级 skill 来自用户 clone 的仓库，可信度与第三方
MCP 工具同级；03-w4-interfaces.md §2 / N15 对 MCP 工具的要求是"默认不进
Agent 白名单，直到用户显式启用"，项目级 skill 目前没有对等处理——这是
H 接线 `_prepare_hermes_home` 之前需要敲定的点（写 `config.yaml` 是 H 的
独占文件，不在这条分支的改动范围），这条分支能做的是：① 改正这句错误
描述，不让下一个读它的人以为已有防护；② 在透明页上把项目级 skill 显式标为
"来自项目仓库、未经确认"（见 `CapabilitySettings.tsx`）。是否要在
`worker_skill_dirs()` 或 H 的接入点加一道真正的门，留给评审/H 决定。

## How a Session's Skill dirs reach the worker (source-checked, not assumed)

Read from the installed `hermes-agent` checkout (not the PyPI page, not
memory):

- `hermes_constants.py:1138 get_skills_dir()` — the *local* skills dir a
  worker's Hermes instance uses is always `<HERMES_HOME>/skills` (no
  override). Jones's per-worker `HERMES_HOME` is a fresh directory `H`
  (`workers/manager.py::_prepare_hermes_home`) wipes and recreates on every
  worker (re)spawn — so symlinking/copying INTO `<HERMES_HOME>/skills` would
  need to happen on every single worker start, and would collide with
  whatever Hermes itself seeds there ("seeded from bundled" per
  `tools/skills_tool.py:60`'s comment).
- `agent/skill_utils.py:337 get_external_skills_dirs()` — reads
  `config.yaml`'s `skills.external_dirs` (a list of paths, `~`/`${VAR}`
  expanded, resolved relative to `HERMES_HOME`), validates each is an
  existing directory, and returns them as-is; entries equal to the local
  skills dir are skipped.
- `tools/skills_tool.py:170 _skill_search_dirs()` /
  `agent/skill_utils.py:761 iter_skill_index_files()` — every configured
  external dir is scanned the SAME way as the local skills dir: recursively
  for `SKILL.md` (minus a small excluded-dirs list — VCS/venv/cache dirs —
  and the `references/templates/assets/scripts` support subdirs), first-wins
  by frontmatter `name`/dir name, **in the order the dirs are listed** (a
  dir earlier in the list wins a name collision against one later).

Conclusion (the "选侵入最小的" call this section asks K to make): **write
`skills.external_dirs` into the worker's `config.yaml`**, pointing straight
at Jones's own three directories — no copying, no symlinking, no touching
`<HERMES_HOME>/skills` at all. This is strictly less invasive than the
symlink alternative (no filesystem mutation beyond the one config.yaml write
`_prepare_hermes_home` already does; no risk of a stale symlink surviving a
`HERMES_HOME` wipe or racing the "seeded from bundled" copy; Jones's own
directories stay the single source of truth Hermes reads fresh every scan).

`worker_skill_dirs(ctx, session)` below returns the ordered list H should
write as `skills.external_dirs` (highest-precedence dir first — see its
docstring for the exact precedence rule); **the actual `config.yaml` write is
H's own `_prepare_hermes_home` (03-w4-interfaces.md §1's ownership table)**,
not implemented here.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jones_daemon import paths

# Empty in W4 — "内置 Skill（办公文档、媒体生成）在 W5 单独 Issue" (issue #18's
# own 备注). The directory itself must exist so `worker_skill_dirs()` can
# treat it exactly like the other two tiers (an existing, possibly-empty dir)
# rather than special-casing "builtin isn't there yet".
BUNDLED_SKILLS_DIR = Path(__file__).resolve().parent / "bundled"

# Directory names a scan never descends into: VCS/dependency/cache noise, plus
# Hermes's own progressive-disclosure support dirs (`references/` etc.) so a
# stray `SKILL.md`-shaped file inside one of those doesn't get listed as its
# own skill (mirrors, loosely, `agent.skill_utils.EXCLUDED_SKILL_DIRS` /
# `SKILL_SUPPORT_DIRS` in the installed hermes-agent — see module docstring
# for why this is a preview scan, not a claim of exact parity).
_EXCLUDED_DIR_NAMES = frozenset({
    ".git", ".github", ".hub", ".venv", "venv", "node_modules", "__pycache__",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", "references", "templates",
    "assets", "scripts",
})


def _iter_skill_md_files(root: Path):
    """`os.walk` with in-place `dirnames` pruning, not `Path.rglob` — 评审第
    1 轮 #3: `rglob("SKILL.md")` walks the ENTIRE subtree before
    `_EXCLUDED_DIR_NAMES` ever gets a look (filtering happened on the
    *results*, after rglob had already descended into every `node_modules`/
    `.venv`/`__pycache__` it found), which is the exact noise this exclusion
    list exists to avoid. Pruning `dirnames` during the walk means an
    excluded directory is never opened at all."""
    if not root.is_dir():
        return
    hits: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in _EXCLUDED_DIR_NAMES)
        if "SKILL.md" in filenames:
            hits.append(Path(dirpath) / "SKILL.md")
    yield from sorted(hits)


def _parse_frontmatter(text: str) -> tuple[dict[str, str], str | None]:
    """Minimal `---\\nkey: value\\n---` YAML frontmatter reader — daemon/pyproject.toml
    has no YAML dependency for this package to reach for (DEV.md 工程原则 #6:
    "不引入...重型抽象" — a real skill's frontmatter is flat `name`/`description`/
    `tags` scalars, see any `SKILL.md` under the installed hermes-agent's
    `skills/`; nested/list YAML isn't needed for a name+description preview
    and this deliberately does not attempt it). Returns `(fields, error)` —
    `error` is set (fields possibly partial) whenever the block can't be
    read, so a malformed skill is surfaced as `valid: False` with a reason
    rather than silently skipped (DEV.md 工程原则 #4 / 03-w4-interfaces.md §6:
    "Skill 格式错 → 列出并标 invalid，不静默跳过")."""
    if not text.startswith("---"):
        return {}, "SKILL.md 缺少开头的 YAML frontmatter（'---'）"
    end = text.find("\n---", 3)
    if end == -1:
        return {}, "SKILL.md frontmatter 缺少结束的 '---'"
    fields: dict[str, str] = {}
    for line in text[3:end].strip("\n").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or ":" not in stripped:
            continue
        key, _, value = stripped.partition(":")
        fields[key.strip()] = value.strip().strip('"').strip("'")
    return fields, None


@dataclass(frozen=True)
class SkillEntry:
    name: str
    description: str
    tier: str  # "project" | "user" | "builtin"
    source_path: str
    valid: bool
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "tier": self.tier,
            "source_path": self.source_path,
            "valid": self.valid,
            "error": self.error,
        }


def _scan_tier(root: Path, tier: str) -> list[SkillEntry]:
    entries: list[SkillEntry] = []
    for skill_md in _iter_skill_md_files(root):
        try:
            text = skill_md.read_text(encoding="utf-8")
        except OSError as exc:
            entries.append(SkillEntry(
                name=skill_md.parent.name, description="", tier=tier,
                source_path=str(skill_md), valid=False, error=f"读取失败：{exc}",
            ))
            continue
        fields, error = _parse_frontmatter(text)
        name = fields.get("name") or skill_md.parent.name
        if error is not None:
            entries.append(SkillEntry(
                name=name, description="", tier=tier,
                source_path=str(skill_md), valid=False, error=error,
            ))
            continue
        entries.append(SkillEntry(
            name=name, description=fields.get("description", ""), tier=tier,
            source_path=str(skill_md), valid=True,
        ))
    return entries


def list_skills(*, project_path: str | None) -> list[dict[str, Any]]:
    """Skills visible to a project, most-specific tier wins a name collision
    (project > user > builtin — the same "更具体覆盖更通用" precedence
    `worker_skill_dirs()` hands Hermes, see that function's docstring;
    03-w4-interfaces.md §5 names the three tiers but does not itself state an
    override order, so this ordering is this module's own design decision —
    called out in the PR report, not asserted as contract). An INVALID skill
    (frontmatter didn't parse) is always listed, never silently dropped, and
    never participates in name-collision shadowing (its `name` fallback is
    just the directory name, not a claim about what a real Agent would see).

    `project_path=None` (no Project resolved for this context) skips the
    project tier entirely rather than guessing a path.
    """
    tiers: list[tuple[Path, str]] = []
    if project_path is not None:
        # create=False: this is a read-only scan (paths.py:115-123's contract,
        # mirrored by agents/store.py) — it must not resurrect
        # `<project_path>/.jones/skills` for a project directory the user has
        # since deleted or unmounted (评审第 1 轮 #2). `_iter_skill_md_files`
        # already treats a non-existent root as "no skills here", not an error.
        tiers.append((paths.project_skills_dir(project_path, create=False), "project"))
    tiers.append((paths.skills_dir(), "user"))
    tiers.append((BUNDLED_SKILLS_DIR, "builtin"))

    seen_dirs: set[Path] = set()
    seen_names: set[str] = set()
    result: list[dict[str, Any]] = []
    for root, tier in tiers:
        resolved = root.resolve()
        if resolved in seen_dirs:
            # The default Project's project_skills_dir IS the user skills dir
            # (both resolve to `<home>/.jones/skills` — `projects/bootstrap.py`
            # seeds the default Project's path at `Path.home()`) — scanning it
            # twice would double-list every skill in it.
            continue
        seen_dirs.add(resolved)
        for entry in _scan_tier(root, tier):
            if entry.valid:
                if entry.name in seen_names:
                    continue
                seen_names.add(entry.name)
            result.append(entry.to_dict())
    return result


def worker_skill_dirs(ctx: Any, session: dict[str, Any]) -> list[Path]:
    """The Skill search roots H's `_prepare_hermes_home` should write as this
    worker's `skills.external_dirs` (see module docstring for why that's the
    chosen integration point, not a symlink into `<HERMES_HOME>/skills`).

    Order = precedence (Hermes scans `external_dirs` in list order,
    first-wins by name, per `tools/skills_tool.py::_find_all_skills` —
    verified against the installed checkout): project dir first (most
    specific), then the user dir, then the builtin dir. Only directories that
    actually exist are returned — checked with `is_dir()` for every tier
    (not relied on as a side effect of `mkdir`-on-access: the project tier is
    resolved with `create=False`, same read-only contract as `list_skills()`,
    so a deleted/unmounted project directory is genuinely absent here, not
    silently recreated — 评审第 1 轮 #2). An empty/missing builtin tier in W4
    is normal, not an error (see `BUNDLED_SKILLS_DIR`'s comment); the default
    Project's project dir is deduplicated against the user dir since they're
    literally the same path (see `list_skills()`'s matching comment).

    Must be called on the DB thread (`store.run_in_db_thread`) — same
    constraint as `permissions/gate_config.py::build()`, which this function's
    signature and "resolve the project via a synchronous `ctx.db` query"
    pattern deliberately mirrors. `session["project_id"]` missing/unresolvable
    is NOT swallowed here: `ProjectService.get()` raises `RpcError(NOT_FOUND)`
    for a Session whose Project was deleted out from under it, and that's
    the honest signal to propagate (DEV.md 工程原则 #4) — not a silent
    "no project tier" degradation a caller could mistake for "this project
    genuinely has no skills".
    """
    from jones_daemon.projects.service import ProjectService

    project_id = session.get("project_id")
    project_path: str | None = None
    if project_id:
        project_path = ProjectService(ctx.db).get(project_id)["path"]

    candidates: list[Path] = []
    if project_path is not None:
        project_dir = paths.project_skills_dir(project_path, create=False)
        if project_dir.is_dir():
            candidates.append(project_dir)
    # skills_dir() always ensures ~/.jones/skills exists (same as every other
    # user-level accessor in paths.py) — it is not a project path a user can
    # delete/unmount out from under this scan, so no create=False here.
    candidates.append(paths.skills_dir())
    if BUNDLED_SKILLS_DIR.is_dir():
        candidates.append(BUNDLED_SKILLS_DIR)

    seen: set[Path] = set()
    ordered: list[Path] = []
    for d in candidates:
        resolved = d.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        ordered.append(d)
    return ordered
