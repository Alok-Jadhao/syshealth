"""The catalogue of things that may be done to a machine.

This file is the complete list. If a remediation is not here, no part of the
system can perform it — not the reasoner, not the API, not an operator
clicking approve. Adding a capability means adding a specification here, with
a tier and an argument schema, in a diff someone reviews.

There is deliberately no ``run_command``, no ``exec``, and no action taking a
free-text string that reaches a shell. That omission is the single most
important safety property in the package, and it is preserved by construction
rather than by validation: the executor looks a name up in this registry and
calls a Python function with typed arguments. There is no code path from a
model's output to a subprocess argument vector.

Handlers live on the node side (``executor.py``). This module is pure data
plus validation, so the server can reason about actions it will never run.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ..mcp.tools import Tier, ToolInputError

# Deliberately strict. Service and container names are identifiers, and
# anything outside this alphabet is far more likely to be an injection attempt
# than a real unit name.
SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.@-]{0,127}$")


@dataclass(frozen=True)
class ArgSpec:
    """One argument, and what it is allowed to be."""

    name: str
    kind: type
    description: str
    required: bool = True
    default: Any = None
    choices: tuple[str, ...] | None = None
    pattern: re.Pattern[str] | None = None
    minimum: float | None = None
    maximum: float | None = None

    def validate(self, value: Any) -> Any:
        if value is None:
            if self.required:
                raise ToolInputError(f"{self.name} is required")
            return self.default

        # bool is a subclass of int; a caller passing True for a count is
        # making a mistake, not supplying 1.
        if isinstance(value, bool) is not (self.kind is bool):
            raise ToolInputError(f"{self.name} must be {self.kind.__name__}")
        if not isinstance(value, self.kind):
            raise ToolInputError(
                f"{self.name} must be {self.kind.__name__}, got {type(value).__name__}"
            )

        if self.choices is not None and value not in self.choices:
            raise ToolInputError(
                f"{self.name} must be one of {', '.join(self.choices)}; got {value!r}"
            )
        if self.pattern is not None and not self.pattern.match(value):
            raise ToolInputError(
                f"{self.name}={value!r} is not a valid name. Allowed: letters, "
                "digits, and _ . @ - up to 128 characters"
            )
        if self.minimum is not None and value < self.minimum:
            raise ToolInputError(f"{self.name} must be >= {self.minimum}")
        if self.maximum is not None and value > self.maximum:
            raise ToolInputError(f"{self.name} must be <= {self.maximum}")
        return value


@dataclass(frozen=True)
class ActionSpec:
    """One thing that may be done, and everything needed to decide about it."""

    name: str
    tier: Tier
    summary: str
    args: tuple[ArgSpec, ...] = ()

    # What should be true again once this has worked. Verification measures
    # it; the action does not get to declare its own success.
    expects: str = ""

    # Whether running it twice in a row is harmless. Used when deciding
    # whether a timed-out action may be retried at all.
    idempotent: bool = True

    # Plain-language consequence, shown to whoever is asked to approve.
    blast_radius: str = ""

    def validate(self, args: dict[str, Any] | None) -> dict[str, Any]:
        """Check and normalise arguments. Unknown keys are refused."""
        supplied = dict(args or {})
        known = {spec.name for spec in self.args}

        unexpected = sorted(set(supplied) - known)
        if unexpected:
            expected = ", ".join(sorted(known)) or "none"
            raise ToolInputError(
                f"{self.name} got unexpected argument(s): {', '.join(unexpected)}. "
                f"Expected: {expected}"
            )

        return {spec.name: spec.validate(supplied.get(spec.name)) for spec in self.args}

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "tier": self.tier.value,
            "summary": self.summary,
            "expects": self.expects,
            "blast_radius": self.blast_radius,
            "idempotent": self.idempotent,
            "args": [
                {
                    "name": a.name,
                    "type": a.kind.__name__,
                    "required": a.required,
                    "description": a.description,
                }
                for a in self.args
            ],
        }


@dataclass(frozen=True)
class Action:
    """A specific action with specific arguments, ready to be decided about."""

    spec: ActionSpec
    args: dict[str, Any] = field(default_factory=dict)

    @property
    def name(self) -> str:
        return self.spec.name

    @property
    def tier(self) -> Tier:
        return self.spec.tier

    def describe(self) -> str:
        if not self.args:
            return self.name
        rendered = ", ".join(f"{k}={v!r}" for k, v in sorted(self.args.items()))
        return f"{self.name}({rendered})"


# --------------------------------------------------------------- catalogue --

_SPECS: tuple[ActionSpec, ...] = (
    ActionSpec(
        name="restart_service",
        tier=Tier.LOW_RISK,
        summary="Restart one systemd service on the node.",
        args=(
            ArgSpec(
                "service",
                str,
                "systemd unit name, e.g. 'nginx' or 'app.service'",
                pattern=SAFE_NAME,
            ),
        ),
        expects="the service is active again and the pressure it caused subsides",
        blast_radius="that service's requests fail for the length of the restart",
    ),
    ActionSpec(
        name="restart_container",
        tier=Tier.LOW_RISK,
        summary="Restart one container on the node.",
        args=(
            ArgSpec("container", str, "container name or id", pattern=SAFE_NAME),
        ),
        expects="the container is running and healthy, and its memory returns to baseline",
        blast_radius="that container's requests fail for the length of the restart",
    ),
    ActionSpec(
        name="drop_page_cache",
        tier=Tier.LOW_RISK,
        summary="Ask the kernel to drop clean page cache (echo 1 > drop_caches).",
        args=(),
        expects="reclaim pressure falls without a rise in major faults",
        idempotent=True,
        blast_radius=(
            "a temporary IO slowdown while caches refill. Does not free memory "
            "that is actually in use, and is almost never the right fix — high "
            "cache is usually the false alarm, not the fault"
        ),
    ),
    ActionSpec(
        name="clear_temp_files",
        tier=Tier.LOW_RISK,
        summary="Delete files under a specific allowlisted temp directory older than N hours.",
        args=(
            ArgSpec(
                "directory",
                str,
                "which temp directory to clear",
                choices=("/tmp", "/var/tmp", "/var/log/journal"),
            ),
            ArgSpec(
                "older_than_hours",
                int,
                "only remove entries older than this",
                required=False,
                default=24,
                minimum=1,
                maximum=720,
            ),
        ),
        expects="free disk space increases and disk pressure falls",
        idempotent=True,
        blast_radius="files in that directory older than the cutoff are deleted permanently",
    ),
    ActionSpec(
        name="terminate_instance",
        tier=Tier.HIGH_RISK,
        summary="Terminate the EC2 instance backing this node.",
        args=(
            ArgSpec("instance_id", str, "EC2 instance id", pattern=SAFE_NAME),
            ArgSpec("confirm", bool, "must be true; there is no undo", required=True),
        ),
        expects="the node leaves the fleet and a replacement reaches HEALTHY",
        idempotent=False,
        blast_radius=(
            "the instance and everything on local storage is destroyed. "
            "Irreversible."
        ),
    ),
)

ACTIONS: dict[str, ActionSpec] = {spec.name: spec for spec in _SPECS}


def build(name: str, args: dict[str, Any] | None = None) -> Action:
    """Look up an action and validate its arguments, or refuse.

    The only supported way to construct an Action. An unknown name fails here,
    which is what makes "the reasoner cannot invent a capability" true rather
    than merely intended.
    """
    spec = ACTIONS.get(name)
    if spec is None:
        available = ", ".join(sorted(ACTIONS)) or "none"
        raise ToolInputError(
            f"unknown action {name!r}. This system can only perform actions "
            f"from its catalogue: {available}"
        )
    return Action(spec=spec, args=spec.validate(args))


def catalogue(tier: Tier | None = None) -> list[dict[str, Any]]:
    """The catalogue as data, for the API, the dashboard, and the reasoner."""
    return [
        spec.describe()
        for spec in sorted(ACTIONS.values(), key=lambda s: (s.tier.value, s.name))
        if tier is None or spec.tier is tier
    ]
