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


def agents_dir(*, create: bool = True) -> Path:
    """`create=False` returns the path without touching the filesystem — for
    read-only callers (AgentStore.read/list_ids) that must not resurrect a
    directory a user deleted, or fail with PermissionError just from being asked
    where something *would* live (see agents/store.py)."""
    if not create:
        return user_root() / "agents"
    _ensure_root()
    return _ensure(user_root() / "agents")


def skills_dir() -> Path:
    _ensure_root()
    return _ensure(user_root() / "skills")


def projects_dir() -> Path:
    _ensure_root()
    return _ensure(user_root() / "projects")


def project_attachments_dir(project_id: str, *, create: bool = True) -> Path:
    """`<user_root>/projects/<project-id>/` — PRD 10.2's per-Project attachments
    directory (user uploads, agent-generated files/media). Named accessor for what
    `projects/service.py`/`store/maintenance.py` previously spelled out ad hoc as
    `paths.projects_dir() / project_id` — same path, just given a name so the two
    real deletion (Issue #23) has one obvious place to point at rather than
    re-deriving the join. `create=False` mirrors `project_root`'s read-only
    contract: a caller only checking "does this Project have any attachments"
    (e.g. before deleting it) must not resurrect the directory by asking."""
    if not create:
        return projects_dir() / project_id
    return _ensure(projects_dir() / project_id)


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


def project_root(project_path: str | os.PathLike[str], *, create: bool = True) -> Path:
    """Return `<project_path>/.jones`, created on first access.

    `create=False` skips the `mkdir` entirely — a read-only caller asking "where
    would this project's files live" must not have the side effect of recreating
    `<project_path>/.jones` for a project directory the user has since deleted or
    unmounted (see agents/store.py, docs/design/01-w2-interfaces.md §4 review)."""
    path = Path(project_path).expanduser() / ".jones"
    return _ensure(path) if create else path


def project_settings_path(project_path: str | os.PathLike[str], *, create: bool = True) -> Path:
    return project_root(project_path, create=create) / "settings.json"


def project_permissions_path(project_path: str | os.PathLike[str], *, create: bool = True) -> Path:
    return project_root(project_path, create=create) / "permissions.json"


def project_mcp_path(project_path: str | os.PathLike[str], *, create: bool = True) -> Path:
    return project_root(project_path, create=create) / "mcp.json"


def project_agents_dir(project_path: str | os.PathLike[str], *, create: bool = True) -> Path:
    root = project_root(project_path, create=create)
    agents = root / "agents"
    return _ensure(agents) if create else agents


def project_skills_dir(project_path: str | os.PathLike[str], *, create: bool = True) -> Path:
    """`create=False` skips the `mkdir` entirely — same read-only contract as
    `project_root`/`project_agents_dir` above: a caller only scanning for
    Skills (e.g. `skills.list_skills`) must not resurrect `<project_path>/.jones/skills`
    for a project directory the user has since deleted or unmounted."""
    root = project_root(project_path, create=create)
    skills = root / "skills"
    return _ensure(skills) if create else skills


def project_memory_dir(project_path: str | os.PathLike[str]) -> Path:
    return _ensure(project_root(project_path) / "memory")
