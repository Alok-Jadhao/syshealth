"""Configuration.

There are no hardcoded hostnames, IPs or paths anywhere in this package. Every
deployment-specific value arrives here, from the environment or an optional
config file, and nothing else reads ``os.environ`` directly.

Resolution order, lowest priority first:

1. the defaults below
2. ``[syshealth]`` keys in a config file (``--config``, ``SYSHEALTH_CONFIG``,
   ``./syshealth.toml``, then ``~/.config/syshealth/config.toml``)
3. ``SYSHEALTH_*`` environment variables
4. command line flags
"""

from __future__ import annotations

import os
import socket
from dataclasses import dataclass, fields
from pathlib import Path

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - 3.10 fallback
    tomllib = None  # type: ignore[assignment]


CONFIG_SEARCH = (
    Path("syshealth.toml"),
    Path("~/.config/syshealth/config.toml").expanduser(),
)


@dataclass
class Settings:
    # measurement
    proc_root: str = "/proc"
    # Measure one cgroup instead of the whole machine. `container` resolves a
    # Docker container id to its cgroup; `cgroup_root` names the path directly.
    cgroup_root: str = ""
    container: str = ""
    interval_s: float = 2.0
    duration_s: float = 60.0

    # identity
    node_name: str = ""
    instance_type: str = ""

    # agent / server
    server_url: str = ""
    bind_host: str = "127.0.0.1"
    bind_port: int = 5000
    db_path: str = "syshealth.db"

    # catalog
    catalog_path: str = ""

    # autonomous SRE. Every default here is the cautious end: observe only,
    # nothing permitted unattended, two attempts, one node at a time. Enabling
    # autonomy should require saying so, in a file someone reviewed.
    incidents_db: str = "incidents.db"
    mode: str = "OBSERVE"
    reasoner: str = "rules"
    autonomous_actions: str = ""
    max_attempts: int = 2
    cooldown_s: float = 300.0
    max_concurrent_nodes: int = 1
    incident_timeout_s: float = 1800.0
    action_timeout_s: float = 120.0

    # What this node is declared to run, so a remediation has a target that
    # was chosen in advance rather than inferred during an incident.
    managed_service: str = ""
    managed_container: str = ""

    def __post_init__(self) -> None:
        if not self.node_name:
            self.node_name = socket.gethostname()


_CASTS = {
    "interval_s": float,
    "duration_s": float,
    "bind_port": int,
    "max_attempts": int,
    "cooldown_s": float,
    "max_concurrent_nodes": int,
    "incident_timeout_s": float,
    "action_timeout_s": float,
}


def load(config_path: str | os.PathLike | None = None, **overrides) -> Settings:
    """Build Settings from file, environment, then explicit overrides."""
    values: dict[str, object] = {}

    for key, value in _from_file(config_path).items():
        values[key] = value

    known = {f.name for f in fields(Settings)}
    for key in known:
        env_value = os.environ.get(f"SYSHEALTH_{key.upper()}")
        if env_value is not None:
            values[key] = env_value

    for key, value in overrides.items():
        if value is not None and key in known:
            values[key] = value

    for key, cast in _CASTS.items():
        if key in values:
            try:
                values[key] = cast(values[key])  # type: ignore[arg-type]
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{key} must be a {cast.__name__}: {values[key]!r}") from exc

    return Settings(**values)  # type: ignore[arg-type]


def _from_file(explicit: str | os.PathLike | None) -> dict:
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit))
    elif env_path := os.environ.get("SYSHEALTH_CONFIG"):
        candidates.append(Path(env_path))
    else:
        candidates.extend(CONFIG_SEARCH)

    for path in candidates:
        if not path.exists():
            continue
        if tomllib is None:
            raise RuntimeError(
                "reading a config file needs Python 3.11+ (tomllib). "
                "Use environment variables instead."
            )
        try:
            data = tomllib.loads(path.read_text())
        except (OSError, ValueError) as exc:
            raise ValueError(f"could not parse {path}: {exc}") from exc
        section = data.get("syshealth", data)
        known = {f.name for f in fields(Settings)}
        return {k: v for k, v in section.items() if k in known}

    return {}
