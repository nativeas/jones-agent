"""Real-Hermes proof for issue #18's own acceptance line: "把
`/Users/nativeas/.hermes/hermes-agent/skills/` 里任选一个 skill 拷进
`~/.jones/skills` 能被列出并被 Agent 引用".

Gated on `JONES_E2E=1` (docs/design/01-w2-interfaces.md §2: "真实 Hermes 的端到
端放 tests/integration/，用 JONES_E2E=1 门控") — no `ANTHROPIC_API_KEY` needed,
unlike `test_real_hermes_e2e.py`: this test never makes a model call, it only
exercises Hermes's own skill-discovery Python functions directly
(`tools.skills_tool.skills_list`/`skill_view`), which is what "被 Agent 引用"
actually reduces to at runtime — the model's tool-calling loop reaches these
same two functions (`SKILLS_LIST_SCHEMA`/`SKILL_VIEW_SCHEMA` in that module),
so proving they see the copied skill *is* proving the Agent can reference it,
without the cost/flakiness of a real model round-trip.

Self-skips when `hermes-agent` isn't importable (`uv sync --group worker` not
run — see daemon/pyproject.toml's `worker` group and docs/DEV.md), same
convention as `test_real_hermes_e2e.py`.

## What this test is proving about `skills/service.py::worker_skill_dirs()`

This is the "选侵入最小的" call `skills/service.py`'s module docstring makes:
write the Jones skills directory into `skills.external_dirs` in the worker's
`config.yaml`, rather than copying/symlinking anything into
`<HERMES_HOME>/skills`. This test constructs a `config.yaml` in exactly that
shape (hand-written YAML — same "no daemon YAML dependency" reasoning
`skills/service.py`'s frontmatter parser docstring gives) and asserts real,
installed Hermes code actually honors it — not a mock, not a re-
implementation of Hermes's scanner.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("JONES_E2E"),
    reason="real-Hermes e2e: set JONES_E2E=1 (and run `uv sync --group worker`) to run",
)

hermes_agent = pytest.importorskip(
    "tools.skills_tool", reason="hermes-agent not installed (`uv sync --group worker`)"
)

HERMES_AGENT_CHECKOUT = Path("/Users/nativeas/.hermes/hermes-agent")
SOURCE_SKILL = HERMES_AGENT_CHECKOUT / "skills" / "productivity" / "weekly-review-planning"


def _write_external_dirs_config(hermes_home: Path, external_dir: Path) -> None:
    hermes_home.mkdir(parents=True, exist_ok=True)
    (hermes_home / "config.yaml").write_text(
        "skills:\n  external_dirs:\n    - " + str(external_dir) + "\n",
        encoding="utf-8",
    )


@pytest.mark.skipif(not SOURCE_SKILL.is_dir(), reason=f"fixture skill not found at {SOURCE_SKILL}")
def test_a_copied_hermes_skill_is_listed_and_loadable_via_external_dirs(tmp_path, monkeypatch):
    import shutil

    from jones_daemon import paths
    from jones_daemon.skills import service

    # Jones's user-level skills dir (docs/design/03-w4-interfaces.md §5) — NOT
    # the real ~/.jones/skills, JONES_HOME keeps this test hermetic.
    monkeypatch.setenv("JONES_HOME", str(tmp_path / "jones_home"))
    jones_skills_dir = paths.skills_dir()
    shutil.copytree(SOURCE_SKILL, jones_skills_dir / SOURCE_SKILL.name)

    # This branch's own scanner already sees it (fast, no Hermes involved) —
    # asserted first so a failure here (vs. below) tells the reader which
    # half broke.
    listed = service.list_skills(project_path=None)
    # Written when the bundled tier was still empty, so this used to assert the
    # whole list equals just the copied skill. #21 then shipped `office-docs`
    # and `media-gen` as bundled Skills, which legitimately show up here too —
    # what this test is about is that a stock Hermes skill copied into Jones's
    # user-level dir gets picked up, so assert that, not the absence of others.
    by_name = {s["name"]: s for s in listed}
    assert "weekly-review-planning" in by_name, f"copied skill not listed; got {sorted(by_name)}"
    assert by_name["weekly-review-planning"]["valid"] is True

    # A fresh, isolated HERMES_HOME with `skills.external_dirs` pointing at
    # Jones's user skills dir — same shape H's `_prepare_hermes_home` should
    # write (see skills/service.py's module docstring), hand-written here
    # since H's own write path hasn't landed in this branch's worktree.
    hermes_home = tmp_path / "hermes_home"
    _write_external_dirs_config(hermes_home, jones_skills_dir)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.delenv("HERMES_YOLO_MODE", raising=False)
    monkeypatch.delenv("HERMES_SAFE_MODE", raising=False)

    # Caches keyed by (dirs, disabled-set, platform) / config file signature —
    # clear them so this test doesn't depend on nothing else in this process
    # having touched Hermes's skill machinery first with a different HERMES_HOME.
    from agent import skill_utils
    from tools import skills_tool

    skill_utils._external_dirs_cache_clear()
    skills_tool._SKILLS_CACHE.clear()

    external_dirs = skill_utils.get_external_skills_dirs()
    assert jones_skills_dir.resolve() in [d.resolve() for d in external_dirs], (
        f"Hermes's own get_external_skills_dirs() did not pick up {jones_skills_dir} "
        f"from config.yaml's skills.external_dirs — got {external_dirs}"
    )

    import json

    listing = json.loads(skills_tool.skills_list())
    assert listing["success"] is True
    names = [s["name"] for s in listing["skills"]]
    assert "weekly-review-planning" in names, (
        f"real hermes-agent skills_list() did not see the copied skill — got {names}"
    )

    # skill_view() is the exact tool call an Agent's model would make to
    # actually load and use the skill ("被 Agent 引用") — proves the content
    # is real and reachable, not just indexed by name.
    viewed = json.loads(skills_tool.skill_view("weekly-review-planning"))
    assert viewed["success"] is True
    assert viewed["name"] == "weekly-review-planning"
    assert "Weekly Review and Planning" in viewed["content"]
    assert viewed["path"]
