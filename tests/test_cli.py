"""CLI entry-point wiring: every tool must be invocable as a module, and
qsub must spawn job.py through the interpreter (not a bare PATH name)."""

import subprocess
import sys

import pytest

MODULES = ["qsub", "qstat", "qdel", "qinfo", "qserver", "job"]


@pytest.mark.parametrize("mod", MODULES)
def test_module_help_exits_zero(mod):
    res = subprocess.run(
        [sys.executable, "-m", f"queueing_tool.{mod}", "--help"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert res.returncode == 0, res.stderr
    assert "usage" in res.stdout.lower()


def test_job_command_uses_interpreter_module_spawn():
    """The bare 'job.py' PATH spawn was the original install trap — the
    packaged qsub must build [sys.executable, -m, queueing_tool.job, ...]."""
    from queueing_tool.qsub import build_job_command

    cmd = build_job_command(
        script=["train.sh", "arg1"],
        server_address=("localhost", 1234),
        block_idx=0,
        job_id=7,
        subtask_id=0,
        depends_on=[5, 6],
        user="kun",
    )
    assert cmd[:3] == [sys.executable, "-m", "queueing_tool.job"]
    assert "--job_id=7" in cmd
    assert "--depends_on=5,6" in cmd
    assert cmd[-2:] == ["train.sh", "arg1"]
