"""Path resolution for the user-level (~/.jones) and project-level (<project>/.jones) roots.

See docs/design/00-foundation.md §6 and PRD §10.2. Directories are created on first
access (not eagerly at import time) so importing this module never touches disk.
"""

from __future__ import annotations

import os
from pathlib import Path


def user_root() -> Path:
    """Return the user-level Jones root, honoring the JONES_HOME override (used by tests)."""
    override = os.environ.get("JONES_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".jones"


def _ensure(path: Path, *, mode: int | None = None) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    if mode is not None:
        # chmod (not the mkdir `mode=` kwarg) so the permission is exact regardless
        # of umask, and applies even if the directory already existed with looser
        # permissions from before this fix.
        path.chmod(mode)
    return path


def _ensure_root() -> Path:
    # macOS home directories default to world-readable (022 umask), so without this
    # any other local account could traverse into ~/.jones and reach daemon.sock —
    # the full RPC surface — and secrets/. Locking the root itself to 0700 is what
    # actually closes that off; a permission on a leaf directory alone wouldn't,
    # since traversal requires +x at every path component.
    return _ensure(user_root(), mode=0o700)


# --- user-level accessors --------------------------------------------------


def db_path() -> Path:
    _ensure_root()
    return user_root() / "jones.db"


def config_dir() -> Path:
    _ensure_root()
    return _ensure(user_root() / "config")


def secrets_dir() -> Path:
    _ensure_root()
    return _ensure(user_root() / "secrets", mode=0o700)


def agents_dir() -> Path:
    _ensure_root()
    return _ensure(user_root() / "agents")


def skills_dir() -> Path:
    _ensure_root()
    return _ensure(user_root() / "skills")


def projects_dir() -> Path:
    _ensure_root()
    return _ensure(user_root() / "projects")


def memory_global_dir() -> Path:
    _ensure_root()
    return _ensure(user_root() / "memory" / "global")


def runtime_dir() -> Path:
    _ensure_root()
    return _ensure(user_root() / "runtime", mode=0o700)


def runs_dir() -> Path:
    _ensure_root()
    return _ensure(user_root() / "runs")


def logs_dir() -> Path:
    _ensure_root()
    return _ensure(user_root() / "logs")


def cache_dir() -> Path:
    _ensure_root()
    return _ensure(user_root() / "cache")


def pid_file() -> Path:
    return runtime_dir() / "daemon.pid"


def sock_file() -> Path:
    return runtime_dir() / "daemon.sock"


# --- project-level accessors -------------------------------------------------


def project_root(project_path: str | os.PathLike[str]) -> Path:
    """Return `<project_path>/.jones`, created on first access."""
    return _ensure(Path(project_path).expanduser() / ".jones")


def project_settings_path(project_path: str | os.PathLike[str]) -> Path:
    return project_root(project_path) / "settings.json"


def project_permissions_path(project_path: str | os.PathLike[str]) -> Path:
    return project_root(project_path) / "permissions.json"


def project_mcp_path(project_path: str | os.PathLike[str]) -> Path:
    return project_root(project_path) / "mcp.json"


def project_agents_dir(project_path: str | os.PathLike[str]) -> Path:
    return _ensure(project_root(project_path) / "agents")


def project_skills_dir(project_path: str | os.PathLike[str]) -> Path:
    return _ensure(project_root(project_path) / "skills")


def project_memory_dir(project_path: str | os.PathLike[str]) -> Path:
    return _ensure(project_root(project_path) / "memory")
