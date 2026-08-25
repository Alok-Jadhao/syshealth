"""The node side: collecting approved actions and running them.

This is the only module in SysHealth that executes anything, and it is written
to be readable in one sitting for that reason.

The safety properties, in order of importance:

**No inbound listener.** The node polls the server for work. It opens no port,
so there is nothing to reach, authenticate against, or exploit. This is a
direct consequence of the previous agent shipping an unauthenticated endpoint
that ran ``stress`` on request; that hole is not being reopened with a
password on it.

**Handlers are looked up, never constructed.** An action name indexes into
``HANDLERS``. A name that is not a key does not run. There is no string that
becomes a command, no ``shell=True`` anywhere in this file, and no handler
that accepts a caller-supplied program name.

**Arguments are re-validated here.** The server validated them; this validates
them again against the same catalogue. A node should not execute something
because a server it talked to said it was fine — that is exactly the trust
this design is trying not to require.

**Everything is bounded.** Every subprocess has a timeout. A hung remediation
becomes a reported failure, not a stuck node.
"""

from __future__ import annotations

import json
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from ..config import Settings
from .actions import ACTIONS

USER_AGENT = "syshealth-executor"


class ExecutionError(RuntimeError):
    """The action could not be carried out. Reported, never raised past the loop."""


def _run(argv: list[str], timeout: float) -> dict[str, Any]:
    """Run one fixed argument vector.

    ``shell=False`` is the default and is never overridden. ``argv`` is built
    from literals plus values that came through ``ActionSpec.validate``, so no
    element is attacker-controlled text.
    """
    if shutil.which(argv[0]) is None:
        raise ExecutionError(f"{argv[0]} is not available on this node")

    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, shell=False, validated args
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ExecutionError(f"{argv[0]} timed out after {timeout:g}s") from exc
    except OSError as exc:
        raise ExecutionError(f"could not run {argv[0]}: {exc}") from exc

    return {
        "argv": argv,
        "returncode": completed.returncode,
        "stdout": completed.stdout[-4000:],
        "stderr": completed.stderr[-4000:],
    }


# ---------------------------------------------------------------- handlers --


def restart_service(service: str, timeout: float) -> dict[str, Any]:
    result = _run(["systemctl", "restart", service], timeout)
    if result["returncode"] != 0:
        raise ExecutionError(f"systemctl restart {service} exited {result['returncode']}")
    status = _run(["systemctl", "is-active", service], timeout)
    result["is_active"] = status["stdout"].strip()
    if result["is_active"] != "active":
        raise ExecutionError(f"{service} is {result['is_active']} after restart")
    return result


def restart_container(container: str, timeout: float) -> dict[str, Any]:
    result = _run(["docker", "restart", container], timeout)
    if result["returncode"] != 0:
        raise ExecutionError(f"docker restart {container} exited {result['returncode']}")
    inspect = _run(
        ["docker", "inspect", "-f", "{{.State.Status}}", container], timeout
    )
    result["state"] = inspect["stdout"].strip()
    if result["state"] != "running":
        raise ExecutionError(f"{container} is {result['state']} after restart")
    return result


def drop_page_cache(timeout: float) -> dict[str, Any]:
    """Write to /proc/sys/vm/drop_caches directly.

    Deliberately not shelled out as ``sh -c 'echo 1 > ...'``. The redirect is
    the only reason anyone reaches for a shell here, and a two-line Python
    write removes the temptation entirely.
    """
    path = Path("/proc/sys/vm/drop_caches")
    try:
        path.write_text("1\n")
    except OSError as exc:
        raise ExecutionError(f"could not drop caches: {exc}") from exc
    return {"wrote": "1", "path": str(path)}


def clear_temp_files(directory: str, older_than_hours: int, timeout: float) -> dict[str, Any]:
    """Delete old files under one of a fixed set of directories.

    ``directory`` came from ``ArgSpec.choices``, so it is one of three
    literals; it cannot be an arbitrary path. Symlinks are skipped and the
    walk never leaves the chosen root, so a symlink planted in /tmp cannot
    make this delete something elsewhere.
    """
    root = Path(directory).resolve()
    if str(root) not in {"/tmp", "/var/tmp", "/var/log/journal"}:
        raise ExecutionError(f"{directory} is not a permitted directory")

    cutoff = time.time() - older_than_hours * 3600
    removed, freed, skipped = 0, 0, 0

    for entry in root.rglob("*"):
        try:
            if entry.is_symlink() or not entry.is_file():
                skipped += 1
                continue
            if not entry.resolve().is_relative_to(root):
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

    return {
        "directory": str(root),
        "older_than_hours": older_than_hours,
        "removed": removed,
        "freed_bytes": freed,
        "skipped": skipped,
    }


def terminate_instance(instance_id: str, confirm: bool, timeout: float) -> dict[str, Any]:
    """Deliberately not implemented on the node.

    A machine must not be able to destroy itself on instruction from a poll
    response. Instance termination belongs in the control plane, against the
    cloud API, with its own credentials and its own audit trail. Leaving this
    unimplemented is the design, not an omission.
    """
    raise ExecutionError(
        "terminate_instance is not executable from the node agent. Instance "
        "lifecycle actions must run in the control plane against the cloud "
        "API, so that a compromised or confused node cannot destroy itself."
    )


HANDLERS = {
    "restart_service": restart_service,
    "restart_container": restart_container,
    "drop_page_cache": drop_page_cache,
    "clear_temp_files": clear_temp_files,
    "terminate_instance": terminate_instance,
}


def execute(name: str, args: dict[str, Any], timeout: float = 120.0) -> dict[str, Any]:
    """Validate and run one action. The single entry point.

    Re-validates against the catalogue even though the server already did.
    """
    spec = ACTIONS.get(name)
    handler = HANDLERS.get(name)
    if spec is None or handler is None:
        raise ExecutionError(f"{name!r} is not an action this node can perform")

    validated = spec.validate(args)
    return handler(timeout=timeout, **validated)


# ------------------------------------------------------------------ poller --


def _post(url: str, payload: dict, timeout: float = 10.0) -> bool:
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return 200 <= response.status < 300
    except (urllib.error.URLError, OSError, ValueError):
        return False


def _get(url: str, timeout: float = 10.0) -> dict | None:
    request = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT}, method="GET"
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if response.status != 200:
                return None
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return None


def run_executor(settings: Settings) -> int:
    """Poll for approved actions, run them, report what happened.

    Runs alongside ``syshealth agent`` on a node that opts into remediation.
    Keeping it a separate process from the telemetry agent is deliberate: a
    node can push measurements without ever being able to receive work, which
    is the configuration most machines should be in.
    """
    base = settings.server_url.rstrip("/")
    if not base:
        print("error: no server URL. Pass --server.", file=sys.stderr)
        return 1

    node = settings.node_name
    poll_url = f"{base}/nodes/{node}/actions/next"
    running = True

    def stop(_signum, _frame):
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    print(
        f"executor for {node} -> {base}, polling every {settings.interval_s:g}s\n"
        f"can perform: {', '.join(sorted(HANDLERS))}"
    )

    while running:
        action = _get(poll_url)
        if not action or not action.get("id"):
            time.sleep(settings.interval_s)
            continue

        action_id = action["id"]
        name = action.get("action", "")
        args = action.get("arguments", {}) or {}
        print(f"executing action {action_id}: {name} {args}")

        try:
            detail = execute(name, args, timeout=settings.action_timeout_s)
            ok = True
        except ExecutionError as exc:
            detail, ok = {"error": str(exc)}, False
        except Exception as exc:  # noqa: BLE001 - never let a handler kill the poller
            detail, ok = {"error": f"unexpected: {exc}"}, False

        print(f"  -> {'ok' if ok else 'failed'}: {detail.get('error', 'done')}")
        if not _post(f"{base}/actions/{action_id}/result", {"ok": ok, "detail": detail}):
            print(f"  warning: could not report result for action {action_id}", file=sys.stderr)

    print("executor stopped")
    return 0
