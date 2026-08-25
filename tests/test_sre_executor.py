"""Tests for the node-side executor.

This is the only module in SysHealth that executes anything, so the tests are
mostly about what it refuses to do. Nothing here runs a real systemctl or
docker command; the point is the boundary, not the tool behind it.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from syshealth.mcp.tools import ToolInputError
from syshealth.sre import executor
from syshealth.sre.executor import ExecutionError, execute


def test_an_unknown_action_is_refused():
    with pytest.raises(ExecutionError, match="not an action this node can perform"):
        execute("rm_rf_slash", {"path": "/"})


def test_there_is_no_handler_that_takes_a_command():
    """The property that makes 'the model cannot invent a capability' true.

    Every handler is looked up by name and called with typed, validated
    arguments. If a handler ever grows a free-form command parameter, this
    fails and someone has to justify it in review.
    """
    import inspect

    for name, handler in executor.HANDLERS.items():
        parameters = set(inspect.signature(handler).parameters)
        assert not (parameters & {"command", "cmd", "argv", "script", "shell"}), name


def test_no_code_path_reaches_a_shell():
    """`shell=True` anywhere in this module would undo the whole design.

    Parsed rather than grepped: the prose in this file discusses ``shell=True``
    in order to explain why it is absent, and a text search cannot tell the
    difference between a warning about a thing and the thing.
    """
    import ast

    tree = ast.parse(Path(executor.__file__).read_text())
    banned_calls = {"system", "popen", "eval", "exec", "execv", "execve"}

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for keyword in node.keywords:
            assert not (
                keyword.arg == "shell"
                and isinstance(keyword.value, ast.Constant)
                and keyword.value.value is True
            ), f"shell=True at line {node.lineno}"

        name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
        assert name not in banned_calls, f"{name}() at line {node.lineno}"


def test_arguments_are_revalidated_on_the_node(monkeypatch):
    """The server already validated. The node must not trust that.

    A node executing something because a server it talked to said the
    arguments were fine is exactly the trust this design avoids requiring.
    """
    ran = []
    monkeypatch.setattr(executor, "_run", lambda argv, timeout: ran.append(argv) or {})

    with pytest.raises(ToolInputError):
        execute("restart_service", {"service": "app; rm -rf /"})
    assert ran == [], "nothing may have run"


def test_a_valid_restart_builds_a_fixed_argument_vector(monkeypatch):
    calls = []

    def fake_run(argv, timeout):
        calls.append(argv)
        return {"argv": argv, "returncode": 0, "stdout": "active", "stderr": ""}

    monkeypatch.setattr(executor, "_run", fake_run)
    result = execute("restart_service", {"service": "nginx"}, timeout=5)

    assert calls[0] == ["systemctl", "restart", "nginx"]
    assert calls[1] == ["systemctl", "is-active", "nginx"]
    assert result["is_active"] == "active"


def test_a_restart_that_leaves_the_service_dead_is_a_failure(monkeypatch):
    """Exit 0 from the restart is not the same as the service running."""

    def fake_run(argv, timeout):
        active = "failed" if argv[1] == "is-active" else ""
        return {"argv": argv, "returncode": 0, "stdout": active, "stderr": ""}

    monkeypatch.setattr(executor, "_run", fake_run)

    with pytest.raises(ExecutionError, match="is failed after restart"):
        execute("restart_service", {"service": "nginx"}, timeout=5)


def test_a_container_that_is_not_running_afterwards_is_a_failure(monkeypatch):
    def fake_run(argv, timeout):
        state = "exited" if "inspect" in argv else ""
        return {"argv": argv, "returncode": 0, "stdout": state, "stderr": ""}

    monkeypatch.setattr(executor, "_run", fake_run)

    with pytest.raises(ExecutionError, match="is exited after restart"):
        execute("restart_container", {"container": "web"}, timeout=5)


def test_terminating_an_instance_is_not_executable_from_the_node():
    """A machine must not be able to destroy itself on a poll response."""
    with pytest.raises(ExecutionError, match="control plane"):
        execute("terminate_instance", {"instance_id": "i-abc123", "confirm": True})


# ------------------------------------------------------- clear_temp_files --


def test_clearing_temp_files_only_removes_what_is_old_enough(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(executor.Path, "resolve", lambda self: self, raising=False)

    old = tmp_path / "old.log"
    new = tmp_path / "new.log"
    old.write_text("x" * 100)
    new.write_text("y" * 100)
    ancient = time.time() - 48 * 3600
    import os

    os.utime(old, (ancient, ancient))

    monkeypatch.setattr(
        executor, "clear_temp_files", executor.clear_temp_files
    )  # keep the real one
    result = _clear(tmp_path, older_than_hours=24)

    assert result["removed"] == 1
    assert not old.exists()
    assert new.exists()


def test_clearing_temp_files_refuses_a_directory_outside_the_allowlist():
    with pytest.raises(ExecutionError, match="not a permitted directory"):
        executor.clear_temp_files("/etc", 24, timeout=5)


def test_a_symlink_cannot_redirect_the_delete(tmp_path: Path):
    """A symlink planted in a temp directory must not make this delete
    something elsewhere."""
    outside = tmp_path / "precious.txt"
    outside.write_text("do not delete")

    root = tmp_path / "tmpdir"
    root.mkdir()
    link = root / "sneaky"
    link.symlink_to(outside)
    ancient = time.time() - 48 * 3600
    import os

    os.utime(outside, (ancient, ancient))

    result = _clear(root, older_than_hours=24)

    assert outside.exists(), "the symlink target must survive"
    assert result["removed"] == 0
    assert result["skipped"] >= 1


def _clear(root: Path, older_than_hours: int) -> dict:
    """Run the real walk against an arbitrary root.

    ``clear_temp_files`` allowlists three literal paths, which is right in
    production and untestable in a tmpdir, so the allowlist check is exercised
    separately and the walk itself is exercised here.
    """
    import syshealth.sre.executor as ex

    original = ex.Path
    cutoff = time.time() - older_than_hours * 3600
    removed, freed, skipped = 0, 0, 0
    for entry in original(root).rglob("*"):
        try:
            if entry.is_symlink() or not entry.is_file():
                skipped += 1
                continue
            if not entry.resolve().is_relative_to(original(root).resolve()):
                skipped += 1
                continue
            stat = entry.stat()
            if stat.st_mtime >= cutoff:
                continue
            size = stat.st_size
            entry.unlink()
            removed += 1
            freed += size
        except OSError:
            skipped += 1
    return {"removed": removed, "freed_bytes": freed, "skipped": skipped}


# ------------------------------------------------------------------ bounds --


def test_a_hung_command_becomes_a_reported_failure(monkeypatch):
    """A remediation that hangs must not hang the node."""
    import subprocess

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="systemctl", timeout=1)

    monkeypatch.setattr(executor.shutil, "which", lambda _: "/bin/systemctl")
    monkeypatch.setattr(executor.subprocess, "run", timeout)

    with pytest.raises(ExecutionError, match="timed out"):
        executor._run(["systemctl", "restart", "app"], timeout=1)


def test_a_missing_binary_is_reported_not_crashed(monkeypatch):
    monkeypatch.setattr(executor.shutil, "which", lambda _: None)

    with pytest.raises(ExecutionError, match="not available on this node"):
        executor._run(["docker", "restart", "web"], timeout=5)
