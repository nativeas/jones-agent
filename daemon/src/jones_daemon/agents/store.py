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


def _agents_dir(project_path: str | None) -> Path:
    return paths.project_agents_dir(project_path) if project_path else paths.agents_dir()


def _agent_dir(agent_id: str, *, project_path: str | None) -> Path:
    return _agents_dir(project_path) / agent_id


class AgentStore:
    def read(self, agent_id: str, *, project_path: str | None) -> dict[str, Any] | None:
        file_path = _agent_dir(agent_id, project_path=project_path) / AGENT_FILENAME
        if not file_path.exists():
            return None
        raw = yaml.safe_load(file_path.read_text(encoding="utf-8")) or {}
        return {field: raw.get(field) for field in _FIELDS}

    def write(self, agent: dict[str, Any], *, project_path: str | None) -> None:
        agent_dir = _agent_dir(agent["id"], project_path=project_path)
        agent_dir.mkdir(parents=True, exist_ok=True)
        record = {field: agent.get(field) for field in _FIELDS}
        text = yaml.safe_dump(record, sort_keys=False, allow_unicode=True)
        (agent_dir / AGENT_FILENAME).write_text(text, encoding="utf-8")

    def delete(self, agent_id: str, *, project_path: str | None) -> None:
        agent_dir = _agent_dir(agent_id, project_path=project_path)
        if agent_dir.exists():
            shutil.rmtree(agent_dir)

    def list_ids(self, *, project_path: str | None) -> list[str]:
        base = _agents_dir(project_path)
        if not base.exists():
            return []
        return sorted(
            child.name
            for child in base.iterdir()
            if child.is_dir() and (child / AGENT_FILENAME).exists()
        )
