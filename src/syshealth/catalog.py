"""A small catalog of cloud instance types, used to turn a saturation verdict
into a concrete "run this size instead" recommendation.

The prices here are *reference* on-demand Linux prices for us-east-1 and are
not live. They exist so the tool can show an order-of-magnitude cost delta, not
so it can produce an invoice. Override them for real work::

    syshealth report --catalog ./my-prices.json

or point ``SYSHEALTH_CATALOG`` at the same file. The JSON format is a list of
objects with the same keys as ``InstanceType``.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

PRICE_AS_OF = "reference values, us-east-1 on-demand Linux"
HOURS_PER_MONTH = 730

SIZE_ORDER = (
    "nano",
    "micro",
    "small",
    "medium",
    "large",
    "xlarge",
    "2xlarge",
    "4xlarge",
    "8xlarge",
    "12xlarge",
    "16xlarge",
)


@dataclass(frozen=True)
class InstanceType:
    name: str
    vcpu: int
    ram_gb: float
    usd_per_hour: float

    @property
    def family(self) -> str:
        return self.name.split(".", 1)[0]

    @property
    def size(self) -> str:
        return self.name.split(".", 1)[1] if "." in self.name else self.name

    @property
    def size_rank(self) -> int:
        return SIZE_ORDER.index(self.size) if self.size in SIZE_ORDER else len(SIZE_ORDER)

    @property
    def usd_per_month(self) -> float:
        return self.usd_per_hour * HOURS_PER_MONTH

    @property
    def ram_kb(self) -> int:
        return int(self.ram_gb * 1024 * 1024)


_BUILTIN: tuple[InstanceType, ...] = (
    # Burstable, x86
    InstanceType("t3.nano", 2, 0.5, 0.0052),
    InstanceType("t3.micro", 2, 1, 0.0104),
    InstanceType("t3.small", 2, 2, 0.0208),
    InstanceType("t3.medium", 2, 4, 0.0416),
    InstanceType("t3.large", 2, 8, 0.0832),
    InstanceType("t3.xlarge", 4, 16, 0.1664),
    InstanceType("t3.2xlarge", 8, 32, 0.3328),
    # Burstable, arm64
    InstanceType("t4g.nano", 2, 0.5, 0.0042),
    InstanceType("t4g.micro", 2, 1, 0.0084),
    InstanceType("t4g.small", 2, 2, 0.0168),
    InstanceType("t4g.medium", 2, 4, 0.0336),
    InstanceType("t4g.large", 2, 8, 0.0672),
    InstanceType("t4g.xlarge", 4, 16, 0.1344),
    InstanceType("t4g.2xlarge", 8, 32, 0.2688),
    # General purpose
    InstanceType("m5.large", 2, 8, 0.096),
    InstanceType("m5.xlarge", 4, 16, 0.192),
    InstanceType("m5.2xlarge", 8, 32, 0.384),
    InstanceType("m5.4xlarge", 16, 64, 0.768),
    # Compute optimised
    InstanceType("c5.large", 2, 4, 0.085),
    InstanceType("c5.xlarge", 4, 8, 0.17),
    InstanceType("c5.2xlarge", 8, 16, 0.34),
    InstanceType("c5.4xlarge", 16, 32, 0.68),
    # Memory optimised
    InstanceType("r5.large", 2, 16, 0.126),
    InstanceType("r5.xlarge", 4, 32, 0.252),
    InstanceType("r5.2xlarge", 8, 64, 0.504),
)


class Catalog:
    """Lookup and search over a set of instance types."""

    def __init__(self, types: tuple[InstanceType, ...] = _BUILTIN) -> None:
        self.types = tuple(sorted(types, key=lambda t: (t.ram_gb, t.vcpu, t.usd_per_hour)))
        self._by_name = {t.name: t for t in self.types}

    def get(self, name: str | None) -> InstanceType | None:
        if not name:
            return None
        return self._by_name.get(name)

    def family(self, family: str) -> list[InstanceType]:
        return sorted(
            (t for t in self.types if t.family == family),
            key=lambda t: t.size_rank,
        )

    def smallest_with(
        self,
        ram_gb: float,
        vcpu: int = 1,
        family: str | None = None,
    ) -> InstanceType | None:
        """Cheapest type meeting both floors, preferring the same family."""
        pool = self.family(family) if family else list(self.types)
        fits = [t for t in pool if t.ram_gb >= ram_gb and t.vcpu >= vcpu]
        if not fits and family:
            # Nothing in the family is big enough; widen the search.
            fits = [t for t in self.types if t.ram_gb >= ram_gb and t.vcpu >= vcpu]
        if not fits:
            return None
        return min(fits, key=lambda t: (t.usd_per_hour, t.ram_gb))

    def step_up(self, current: InstanceType, steps: int = 1) -> InstanceType | None:
        """The type ``steps`` sizes larger in the same family."""
        siblings = self.family(current.family)
        try:
            index = siblings.index(current)
        except ValueError:
            return None
        target = index + steps
        if target >= len(siblings):
            return siblings[-1] if siblings[-1] != current else None
        return siblings[target]

    @classmethod
    def load(cls, path: str | os.PathLike | None = None) -> Catalog:
        """Load a catalog from JSON, falling back to the builtin table."""
        source = path or os.environ.get("SYSHEALTH_CATALOG")
        if not source:
            return cls()
        try:
            raw = json.loads(Path(source).read_text())
        except (OSError, ValueError) as exc:
            raise ValueError(f"could not read catalog {source}: {exc}") from exc

        types = tuple(
            InstanceType(
                name=str(entry["name"]),
                vcpu=int(entry["vcpu"]),
                ram_gb=float(entry["ram_gb"]),
                usd_per_hour=float(entry["usd_per_hour"]),
            )
            for entry in raw
        )
        if not types:
            raise ValueError(f"catalog {source} is empty")
        return cls(types)
