import stat
from pathlib import Path

from jones_daemon import paths


def test_user_root_honors_jones_home_override(tmp_path, monkeypatch):
    monkeypatch.setenv("JONES_HOME", str(tmp_path / "custom-home"))
    assert paths.user_root() == tmp_path / "custom-home"


def test_user_root_defaults_to_home_dot_jones(monkeypatch):
    monkeypatch.delenv("JONES_HOME", raising=False)
    assert paths.user_root() == Path.home() / ".jones"


def test_directories_are_created_on_demand(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("JONES_HOME", str(home))
    assert not home.exists()

    runtime = paths.runtime_dir()

    assert runtime.exists()
    assert runtime == home / "runtime"


def test_db_path_creates_user_root_but_not_the_db_file(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("JONES_HOME", str(home))

    db = paths.db_path()

    assert home.exists()
    assert db == home / "jones.db"
    assert not db.exists()  # only the directory is created eagerly, not the file


def test_pid_and_sock_files_live_under_runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("JONES_HOME", str(tmp_path / "home"))
    assert paths.pid_file() == paths.runtime_dir() / "daemon.pid"
    assert paths.sock_file() == paths.runtime_dir() / "daemon.sock"


def test_project_root_is_dot_jones_under_project_path(tmp_path):
    project = tmp_path / "some-project"
    root = paths.project_root(project)
    assert root == project / ".jones"
    assert root.exists()


def test_project_level_accessors_create_their_directories(tmp_path):
    project = tmp_path / "some-project"
    assert paths.project_agents_dir(project) == project / ".jones" / "agents"
    assert paths.project_skills_dir(project).exists()
    assert paths.project_memory_dir(project).exists()


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_user_root_is_locked_down_to_owner_only(tmp_path, monkeypatch):
    # ~/.jones holds daemon.sock and secrets/; macOS home directories default to
    # world-readable (022 umask), so the root itself has to deny other local
    # accounts traversal, not just its sensitive children.
    monkeypatch.setenv("JONES_HOME", str(tmp_path / "home"))
    paths.config_dir()  # any accessor call is enough to trigger root creation
    assert _mode(paths.user_root()) == 0o700


def test_secrets_and_runtime_dirs_are_locked_down_to_owner_only(tmp_path, monkeypatch):
    monkeypatch.setenv("JONES_HOME", str(tmp_path / "home"))
    assert _mode(paths.secrets_dir()) == 0o700
    assert _mode(paths.runtime_dir()) == 0o700


def test_root_permission_is_tightened_even_if_it_pre_existed_looser(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir(mode=0o755)
    monkeypatch.setenv("JONES_HOME", str(home))
    paths.runtime_dir()
    assert _mode(paths.user_root()) == 0o700
