"""Job-log registry: qjob records the absolute log path; qtop resolves it."""

from queueing_tool import job as job_mod
from queueing_tool import qtop


def _point_registry_at(monkeypatch, tmp_path):
    registry = tmp_path / "registry"
    monkeypatch.setattr(job_mod, "LOG_REGISTRY_DIR", str(registry))
    return registry


def test_register_then_find_from_anywhere(tmp_path, monkeypatch):
    _point_registry_at(monkeypatch, tmp_path)
    submit_dir = tmp_path / "project"
    (submit_dir / "q.log").mkdir(parents=True)
    log = submit_dir / "q.log" / "myjob.0000042"
    log.write_text("hello\n")

    monkeypatch.chdir(submit_dir)
    job_mod.register_log_path(42, "myjob")

    # resolution must NOT depend on the cwd anymore
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    assert qtop.find_log("42") == str(log)
    assert qtop.registry_log(42) == str(log)


def test_registry_entry_with_missing_file_falls_back(tmp_path, monkeypatch):
    registry = _point_registry_at(monkeypatch, tmp_path)
    registry.mkdir(parents=True)
    (registry / "0000007").write_text(str(tmp_path / "gone" / "x.0000007") + "\n")
    monkeypatch.chdir(tmp_path)
    assert qtop.registry_log(7) is None
    assert qtop.find_log("7") is None


def test_cwd_walk_fallback_for_unregistered_jobs(tmp_path, monkeypatch):
    _point_registry_at(monkeypatch, tmp_path)
    root = tmp_path / "repo"
    sub = root / "notebooks"
    (root / "q.log").mkdir(parents=True)
    sub.mkdir()
    log = root / "q.log" / "legacy.0000009"
    log.write_text("x\n")

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(sub)
    assert qtop.find_log("9") == str(log)


def test_register_is_best_effort(tmp_path, monkeypatch):
    # unwritable registry must never raise
    monkeypatch.setattr(job_mod, "LOG_REGISTRY_DIR", "/proc/definitely/not/writable")
    monkeypatch.chdir(tmp_path)
    job_mod.register_log_path(1, "j")
