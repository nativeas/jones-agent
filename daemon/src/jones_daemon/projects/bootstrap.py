"""Daemon-startup bootstrap for the default Project/Agent (docs/design/
01-w2-interfaces.md §2/§4, issues #8/#9).

Lives here — in C's own package — rather than as a closure in `__main__.py`.
`__main__.py` is shared across every W2 branch and contractually only allowed
"one line" per module (§0); the actual bootstrap logic doesn't need to be in the
shared file to satisfy that, and keeping it out shrinks the next branch's (A's
sessions/workers) conflict surface with this file to nothing more than one more
import + one more `register()` call.

Must run on the dedicated DB thread (see `store/db.py`'s module docstring) —
callers do `await run_in_db_thread(bootstrap_projects_and_agents, conn)`, not call
this directly from the event loop.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from jones_daemon.agents.service import AgentService
from jones_daemon.projects.service import ProjectService


def bootstrap_projects_and_agents(conn: sqlite3.Connection) -> None:
    ProjectService(conn).ensure_default_project(str(Path.home()))
    agent_service = AgentService(conn)
    agent_service.ensure_default_agent_file()
    agent_service.sync_from_files()
