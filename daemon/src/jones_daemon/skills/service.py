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
and does its frontmatter parse" for the settings page.

**评审第 3 轮 #2/#5 收口**：本模块曾承诺"differ-from-Hermes 的 edge case
写进报告"，但报告里从没真的列过——这里补上，逐条写清是"已对齐"还是"已知
接受的差异"：
- 排除目录名单（`_EXCLUDED_DIR_NAMES`）、`references/templates/assets/
  scripts` 的条件剪枝（`_SKILL_SUPPORT_DIR_NAMES`，仅当当前目录自带
  `SKILL.md` 才剪）、符号链接（`followlinks=True`）——**已对齐**，见
  `_iter_skill_md_files` 与上面两个常量各自的注释。
- **已知接受的差异（未对齐，故意）**：Hermes 的 `_org/` 组织镜像目录
  （`ORG_MIRROR_DIR_NAME`）是 token 门控的——只有 `read_active_org_id()`
  读到的那一个组织子目录会被扫描，未激活的组织镜像即使物理存在也不会被
  Hermes 加载。这个扫描器没有实现这层门控：如果 `~/.jones/skills/_org/`
  下真的出现这种目录结构（目前 Jones 没有任何代码会创建它——组织同步是
  Hermes 自己的功能，Jones 未接入），本扫描器会把未激活组织的 skill 也列
  出来，比 Hermes 实际加载的多列（反方向漂移：多列不存在的，不是漏列
  存在的）。接受理由：① 触发条件（`~/.jones/skills/` 下出现 `_org/` 子目录
  且其中有未激活组织的内容）在 Jones 当前功能集下不会自然发生；② 完整
  实现需要读 `.active_org` marker 并复刻 `ORG_MIRROR_DIR_NAME`/
  `ORG_ACTIVE_MARKER` 一整套逻辑，属于"重写 Hermes 已有工具"的复杂度
  （DEV.md 工程原则 #1），换来的只是一个目前不会触发的差异。若 Jones 之后
  接入组织同步功能，需要回来补这一层。

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

# Directory names a scan never descends into, at ANY depth — VCS/dependency/
# cache noise. **评审第 3 轮 #2/#5**: this set now mirrors the installed
# hermes-agent's `agent/skill_utils.py::EXCLUDED_SKILL_DIRS` exactly (name
# for name — `.archive`/`.curator_backups`/`site-packages`/`.tox`/`.nox` were
# previously missing here, which was reverse drift: this scanner would list a
# skill nested under one of those that Hermes's own walk would never reach).
# `references`/`templates`/`assets`/`scripts` are deliberately NOT in this
# set — see `_SKILL_SUPPORT_DIR_NAMES` below for why they need different,
# conditional treatment instead of unconditional pruning.
_EXCLUDED_DIR_NAMES = frozenset({
    ".git", ".github", ".hub", ".archive", ".curator_backups",
    ".venv", "venv", "node_modules", "site-packages", "__pycache__",
    ".tox", ".nox", ".pytest_cache", ".mypy_cache", ".ruff_cache",
})

# Hermes's own progressive-disclosure support dirs inside a skill package
# (`agent/skill_utils.py::SKILL_SUPPORT_DIRS`) — loaded explicitly via
# `skill_view(skill, file_path=...)`, never scanned as standalone skills.
# **评审第 3 轮 #2/#5 (was a false negative, not just a false positive)**:
# the previous version of this module pruned these names unconditionally at
# any depth, same as `_EXCLUDED_DIR_NAMES` — but Hermes's own
# `iter_skill_index_files` (skill_utils.py:776) only prunes them when the
# directory *currently being walked* has its own `SKILL.md`
# (`has_skill_md and d in SKILL_SUPPORT_DIRS`). A TOP-LEVEL skill legitimately
# named `scripts/` (or `references/`, etc. — an unusual but valid skill name,
# same as any other word) is one Hermes loads and the previous unconditional
# version of this scanner could never list — the exact class of bug the
# transparency page (G21) exists to catch, not merely a cosmetic mismatch.
_SKILL_SUPPORT_DIR_NAMES = frozenset({"references", "templates", "assets", "scripts"})


def _iter_skill_md_files(root: Path):
    """`os.walk` with in-place `dirnames` pruning, not `Path.rglob` — 评审第
    1 轮 #3: `rglob("SKILL.md")` walks the ENTIRE subtree before
    `_EXCLUDED_DIR_NAMES` ever gets a look (filtering happened on the
    *results*, after rglob had already descended into every `node_modules`/
    `.venv`/`__pycache__` it found), which is the exact noise this exclusion
    list exists to avoid. Pruning `dirnames` during the walk means an
    excluded directory is never opened at all.

    评审第 3 轮 #2/#5: `followlinks=True` (was `False`, the `os.walk` default)
    to match Hermes's own `iter_skill_index_files` (skill_utils.py:770) — a
    skill directory reached via a symlink (a common way to reuse an existing
    skill checkout without duplicating it) is one Hermes loads at runtime;
    this scanner must not silently omit it from the transparency page. Same
    section: `_SKILL_SUPPORT_DIR_NAMES` is pruned only when the CURRENT
    directory has its own `SKILL.md`, matching Hermes's conditional pruning
    instead of the previous unconditional-at-any-depth behavior — see that
    set's own comment."""
    if not root.is_dir():
        return
    hits: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=True):
        has_skill_md = "SKILL.md" in filenames
        dirnames[:] = sorted(
            d for d in dirnames
            if d not in _EXCLUDED_DIR_NAMES
            and not (has_skill_md and d in _SKILL_SUPPORT_DIR_NAMES)
        )
        if has_skill_md:
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
        except (OSError, ValueError) as exc:
            # 评审第 3 轮 #4: `ValueError` alongside `OSError` — a non-UTF-8
            # `SKILL.md` raises `UnicodeDecodeError` (a `ValueError` subclass,
            # not an `OSError`), which the previous `except OSError` did not
            # catch: it propagated out of `_scan_tier` → `list_skills()` →
            # the `skill.list` RPC handler as an uncaught INTERNAL_ERROR,
            # taking down the ENTIRE listing (every other, well-formed skill
            # included) instead of surfacing just this one entry as invalid.
            # 03-w4-interfaces.md §6 ("Skill 格式错 → 列出并标 invalid，不
            # 静默跳过") and this function's own docstring promise apply to
            # any unreadable `SKILL.md`, encoding errors included, not only
            # `OSError`.
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


@dataclass(frozen=True)
class WorkerSkillDirs:
    """**评审第 3 轮 #8**: `project`/`trusted` split out instead of one flat
    list — `worker_skill_dirs()` used to return project-tier-then-user-then-
    builtin as a single `list[Path]`, which made including the project tier
    in `skills.external_dirs` an invisible side effect of `for d in
    worker_skill_dirs(...): ...` rather than a conscious choice. The project
    tier is Jones's own least-trusted Skill source (it comes from whatever
    `.jones/skills/` a cloned repo happens to contain — see `service.py`'s
    module docstring, "评审第 1 轮 #5"): a caller must now name `.project`
    explicitly to include it.

    This split does NOT itself decide whether the project tier should be
    on by default — that's still an open product/security decision for H's
    `_prepare_hermes_home` integration and the controller (see this PR's
    report: 项目级 Skill 目前无信任门，对等于 N15 对第三方 MCP 工具"默认不
    启用"的要求）。`worker_skill_dirs()` still has zero callers as of this
    branch, so this is the cheap moment to make that choice a deliberate
    line of code at the call site rather than a silent default baked into
    this function — not a claim that the policy question itself is settled.
    """

    project: Path | None
    trusted: list[Path]

    def all_dirs(self) -> list[Path]:
        """Old flat-list behavior (project first, then `trusted`) for a
        caller/test that explicitly wants "everything, no opinion on trust" —
        NOT what H should reach for without thinking about it (see class
        docstring)."""
        return ([self.project] if self.project is not None else []) + list(self.trusted)


def worker_skill_dirs(ctx: Any, session: dict[str, Any]) -> WorkerSkillDirs:
    """The Skill search roots H's `_prepare_hermes_home` should write as this
    worker's `skills.external_dirs` (see module docstring for why that's the
    chosen integration point, not a symlink into `<HERMES_HOME>/skills`) —
    split into `.project` (the project tier, or `None`) and `.trusted` (user
    then builtin, in precedence order) so including the project tier is a
    conscious choice at the call site (see `WorkerSkillDirs`'s docstring,
    评审第 3 轮 #8).

    Precedence within `.trusted` (Hermes scans `external_dirs` in list order,
    first-wins by name, per `tools/skills_tool.py::_find_all_skills` —
    verified against the installed checkout): user dir before builtin dir.
    `.project`, if a caller chooses to prepend it, is more specific than
    either. Only directories that actually exist are returned — checked with
    `is_dir()` for every tier (not relied on as a side effect of `mkdir`-on-
    access: the project tier is resolved with `create=False`, same read-only
    contract as `list_skills()`, so a deleted/unmounted project directory is
    genuinely absent here, not silently recreated — 评审第 1 轮 #2). An
    empty/missing builtin tier in W4 is normal, not an error (see
    `BUNDLED_SKILLS_DIR`'s comment); when the default Project's directory is
    literally the user skills dir (see `list_skills()`'s matching comment),
    `.project` comes back `None` and the shared directory surfaces once, via
    `.trusted`, not twice.

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

    project_dir: Path | None = None
    if project_path is not None:
        candidate = paths.project_skills_dir(project_path, create=False)
        if candidate.is_dir():
            project_dir = candidate

    # skills_dir() always ensures ~/.jones/skills exists (same as every other
    # user-level accessor in paths.py) — it is not a project path a user can
    # delete/unmount out from under this scan, so no create=False here.
    trusted_candidates: list[Path] = [paths.skills_dir()]
    if BUNDLED_SKILLS_DIR.is_dir():
        trusted_candidates.append(BUNDLED_SKILLS_DIR)

    seen: set[Path] = set()
    if project_dir is not None:
        seen.add(project_dir.resolve())
    trusted: list[Path] = []
    for d in trusted_candidates:
        resolved = d.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        trusted.append(d)

    return WorkerSkillDirs(project=project_dir, trusted=trusted)
