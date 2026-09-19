"""File storage for Agent definitions — the source of truth (design §4: "文件为
事实源 + 表索引"). User-level agents live at `~/.jones/agents/<id>/agent.yaml`,
project-level at `<project>/.jones/agents/<id>/agent.yaml` (PRD 10.2).

`AgentService` (service.py) is what keeps the `agents` table index in sync with
what's on disk; this module only knows about files.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import yaml

from jones_daemon import paths
from jones_daemon.logging import get_logger

logger = get_logger("agents")

AGENT_FILENAME = "agent.yaml"

# The six fields FR03 requires be editable (人设/语气/原则/工具白名单/Skill 集合/
# 模型偏好), plus id/created_at/updated_at bookkeeping. This is the full on-disk
# shape of agent.yaml.
_FIELDS = (
    "id",
    "name",
    "persona",
    "tone",
    "principles",
    "tool_allowlist",
    "skills",
    "model_pref",
    "created_at",
    "updated_at",
)


def _agents_dir(project_path: str | None, *, create: bool = True) -> Path:
    if project_path:
        return paths.project_agents_dir(project_path, create=create)
    return paths.agents_dir(create=create)


def _agent_dir(agent_id: str, *, project_path: str | None, create: bool = True) -> Path:
    return _agents_dir(project_path, create=create) / agent_id


class AgentStore:
    def read(self, agent_id: str, *, project_path: str | None) -> dict[str, Any] | None:
        # Read-only: never mkdir the agents tree just to look inside it (see
        # paths.py — a deleted/unmounted project directory must stay deleted, not
        # get its `.jones/agents/` resurrected by a startup scan).
        file_path = (
            _agent_dir(agent_id, project_path=project_path, create=False) / AGENT_FILENAME
        )
        if not file_path.exists():
            return None
        try:
            raw = yaml.safe_load(file_path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            logger.warning(
                "failed to parse agent.yaml, skipping",
                extra={"detail": {"path": str(file_path), "error": str(exc)}},
            )
            return None
        if not isinstance(raw, dict):
            logger.warning(
                "agent.yaml did not contain a mapping, skipping",
                extra={"detail": {"path": str(file_path)}},
            )
            return None
        return {field: raw.get(field) for field in _FIELDS}

    def write(self, agent: dict[str, Any], *, project_path: str | None) -> None:
        agent_dir = _agent_dir(agent["id"], project_path=project_path)  # create=True: writing
        agent_dir.mkdir(parents=True, exist_ok=True)
        record = {field: agent.get(field) for field in _FIELDS}
        text = yaml.safe_dump(record, sort_keys=False, allow_unicode=True)
        (agent_dir / AGENT_FILENAME).write_text(text, encoding="utf-8")

    def delete(self, agent_id: str, *, project_path: str | None) -> None:
        agent_dir = _agent_dir(agent_id, project_path=project_path, create=False)
        if agent_dir.exists():
            shutil.rmtree(agent_dir)

    def list_ids(self, *, project_path: str | None) -> list[str]:
        base = _agents_dir(project_path, create=False)
        if not base.exists():
            return []
        return sorted(
            child.name
            for child in base.iterdir()
            if child.is_dir() and (child / AGENT_FILENAME).exists()
        )
