"""The push agent.

Deliberately built on ``urllib`` rather than ``requests`` so that the agent —
the thing that has to be installed on every machine in a fleet — has no
dependencies beyond the standard library.

Two things the previous agent did that this one does not:

* It shipped a control endpoint that ran ``stress`` on receipt of an unauthenticated
  POST. That is a remote denial-of-service primitive listening on 0.0.0.0, and it
  existed only to make a dashboard button work. Load generation belongs in the
  operator's hands, not in a daemon: use ``syshealth profile -- stress ...``.
* It defaulted to a hardcoded server IP, so a fresh clone silently pushed
  measurements at a machine that no longer belonged to anyone.
"""

from __future__ import annotations

import json
import signal
import sys
import time
import urllib.error
import urllib.request

from .config import Settings
from .procfs import ProcReader
from .sampler import Sampler

USER_AGENT = "syshealth-agent"


def push(url: str, payload: dict, timeout: float = 5.0) -> bool:
    """POST one measurement. Returns whether it landed.

    Never raises: an agent that dies because the server was briefly
    unreachable is worse than useless, because it stops collecting exactly
    when something interesting is happening.
    """
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


def _reader(settings: Settings):
    """Whole machine, or one cgroup.

    ``/proc/pressure`` is host-wide, so an agent inside a container either
    sees nothing or sees the host and reports it as the container. Measuring a
    cgroup instead is what makes "which container saturated this box?" a
    question with an answer.
    """
    if settings.container:
        from .cgroup import for_container

        return for_container(settings.container)
    if settings.cgroup_root:
        from .cgroup import CgroupReader

        return CgroupReader(settings.cgroup_root, proc_root=settings.proc_root)
    return ProcReader(settings.proc_root)


def run_agent(settings: Settings) -> int:
    try:
        reader = _reader(settings)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3

    if not reader.has_psi():
        print(f"error: {reader.missing_psi_reason()}", file=sys.stderr)
        return 3

    endpoint = settings.server_url.rstrip("/")
    if not endpoint.endswith("/metrics"):
        endpoint += "/metrics"

    running = True

    def stop(_signum, _frame):
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    print(
        f"agent {settings.node_name} -> {endpoint} "
        f"every {settings.interval_s:g}s"
    )

    sampler = Sampler(reader)
    sampler.tick()

    # Backoff so that a server outage does not turn the whole fleet into a
    # retry storm the moment it comes back.
    failures = 0
    while running:
        time.sleep(settings.interval_s)
        sample = sampler.tick()
        if sample is None:
            continue

        payload = {
            "node": settings.node_name,
            "instance_type": settings.instance_type or None,
            "agent_version": __import__("syshealth").__version__,
            "sample": sample.to_dict(),
        }

        if push(endpoint, payload):
            if failures:
                print(f"reconnected after {failures} failed push(es)")
            failures = 0
        else:
            failures += 1
            if failures in (1, 5, 20) or failures % 100 == 0:
                print(f"warning: push failed ({failures} in a row)", file=sys.stderr)
            time.sleep(min(2 ** min(failures, 5), 30))

    print("agent stopped")
    return 0
