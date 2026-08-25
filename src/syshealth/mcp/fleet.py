"""Tools over the fleet store — the telemetry the agents already push.

Phase 1's tools measure one window on one machine. That is the weakest signal
the project produces: a two-second window on a thrashing box can easily read
calm, and one that reads alarming may be a single spike. The fleet store holds
every node's history, and ``summarise`` already folds it into percentiles, so
these tools answer the same questions with far better evidence — and across
every machine rather than the one the server happens to run on.

Nothing here re-implements analysis. ``summarise`` and ``evaluate`` are the
same functions the CLI and the Flask API call; these tools compose them and
shape the result for a reader that needs to reason about it rather than render
it. The Flask endpoint's payload is deliberately not reused: it is shaped for
a dashboard row, and an agent needs the evidence, not the headline.

Like everything in this package, this module imports no MCP SDK.
"""

from __future__ import annotations

from typing import Any

from ..analysis import RunSummary, State, Thresholds, summarise
from ..catalog import Catalog
from ..models import Interval
from ..rightsize import Sizing, Verdict, evaluate
from ..store import Store
from .tools import Tier, Tool, ToolInputError, compact

# A node pushing every 2s fills this in under two hours. Enough history for a
# percentile to mean something, bounded so one call cannot read a whole day of
# a chatty fleet into memory.
MAX_SAMPLES = 2000
DEFAULT_SAMPLES = 600

# Matches the Flask API, so "online" means the same thing in both places.
ONLINE_WINDOW_S = 20.0


def _require_node(store: Store, node: str) -> dict[str, Any]:
    """Resolve a node name, or fail with something the caller can act on."""
    if not isinstance(node, str) or not node.strip():
        raise ToolInputError("node must be a non-empty string")

    known = {row["node"]: row for row in store.nodes(ONLINE_WINDOW_S)}
    if node not in known:
        names = ", ".join(sorted(known)) or "none"
        raise ToolInputError(f"unknown node {node!r}. Known nodes: {names}")
    return known[node]


def _resources(summary: RunSummary) -> dict[str, Any]:
    return {
        name: {
            "state": res.state.value,
            "some_p50_pct": compact(res.some_p50),
            "some_p95_pct": compact(res.some_p95),
            "some_max_pct": compact(res.some_max),
            "full_max_pct": compact(res.full_max),
            "stalled_seconds": compact(res.stalled_seconds, 1),
        }
        for name, res in summary.resources.items()
    }


def _instance(kind) -> dict[str, Any] | None:
    if kind is None:
        return None
    return {
        "name": kind.name,
        "vcpu": kind.vcpu,
        "ram_gb": kind.ram_gb,
        "usd_per_month": compact(kind.usd_per_month),
    }


def _verdict_payload(node: str, verdict: Verdict) -> dict[str, Any]:
    """The verdict with its reasoning kept separate from its conclusion.

    ``reasons`` is why the engine decided; ``evidence`` is the measurements it
    decided from; ``caveats`` is what would undermine it. Keeping the three
    apart is what lets a reader check the conclusion instead of taking it.
    """
    return {
        "node": node,
        "sizing": verdict.sizing.value,
        "confidence": verdict.confidence.value,
        "headline": verdict.headline,
        "current": _instance(verdict.current),
        "recommended": _instance(verdict.recommended),
        "monthly_delta_usd": compact(verdict.monthly_delta_usd),
        "annual_delta_usd": compact(verdict.monthly_delta_usd * 12),
        "peak_working_set_gb": compact(verdict.peak_working_set_gb, 3),
        "reasons": list(verdict.reasons),
        "evidence": list(verdict.evidence),
        "caveats": list(verdict.caveats),
    }


def build_fleet_tools(
    store: Store,
    catalog: Catalog | None = None,
    thresholds: Thresholds | None = None,
) -> dict[str, Tool]:
    """Bind the read-only fleet tools to one store."""
    cat = catalog or Catalog()
    t = thresholds or Thresholds()

    def _history(node: str, max_samples: int) -> tuple[dict, list[Interval], RunSummary]:
        if isinstance(max_samples, bool) or not isinstance(max_samples, int):
            raise ToolInputError("max_samples must be an integer")
        if not 1 <= max_samples <= MAX_SAMPLES:
            raise ToolInputError(
                f"max_samples must be between 1 and {MAX_SAMPLES}, got {max_samples}"
            )

        meta = _require_node(store, node)
        raw = store.samples(node, limit=max_samples)
        if not raw:
            raise ToolInputError(
                f"node {node!r} is known but has no samples within retention. "
                "It may have stopped pushing; check `online` in list_nodes."
            )
        samples = [Interval.from_dict(row) for row in raw]
        return meta, samples, summarise(samples, thresholds=t, label=node)

    # -------------------------------------------------------------- nodes --

    def list_nodes() -> dict[str, Any]:
        rows = store.nodes(ONLINE_WINDOW_S)
        return {
            "count": len(rows),
            "online": sum(1 for r in rows if r["online"]),
            "nodes": [
                {
                    "node": r["node"],
                    "instance_type": r["instance_type"],
                    "online": r["online"],
                    "seconds_since_last_sample": r["seconds_since"],
                }
                for r in rows
            ],
            "note": (
                "A node is online if it pushed within the last "
                f"{ONLINE_WINDOW_S:g}s. An offline node is not necessarily "
                "unhealthy — the agent may be stopped — but its stored history "
                "is stale, and any health call on it describes the past."
            ),
        }

    # ------------------------------------------------------------- health --

    def get_node_health(node: str, max_samples: int = DEFAULT_SAMPLES) -> dict[str, Any]:
        meta, samples, summary = _history(node, max_samples)

        result: dict[str, Any] = {
            "node": node,
            "instance_type": meta["instance_type"],
            "online": meta["online"],
            "seconds_since_last_sample": meta["seconds_since"],
            "window": {
                "samples": summary.samples,
                "duration_s": compact(summary.duration_s, 1),
            },
            "psi_available": summary.psi_available,
            "state": summary.state.value,
            "bottleneck": summary.bottleneck if summary.state is not State.HEALTHY else None,
            "resources": _resources(summary),
            "utilisation": {
                "working_set_pct_mean": compact(summary.mem_used_pct_mean),
                "working_set_pct_max": compact(summary.mem_used_pct_max),
                "naive_used_pct_max": compact(summary.mem_naive_used_pct_max),
                "swap_used_pct_max": compact(summary.swap_used_pct_max),
                "cpu_busy_pct_mean": compact(summary.cpu_busy_pct_mean),
                "cpu_busy_pct_max": compact(summary.cpu_busy_pct_max),
                "total_gb": compact(summary.mem_total_kb / (1024 * 1024), 3),
            },
            "reclaim": {
                "direct_reclaim_total": summary.direct_reclaim_total,
                "direct_reclaim_per_s": compact(summary.direct_reclaim_per_s),
                "major_faults_total": summary.major_faults_total,
                "swap_in_total": summary.swap_in_total,
                "oom_kills": summary.oom_kills,
            },
            "divergence_pct_points": compact(summary.divergence),
            "note": (
                f"State comes from the p{t.headline_percentile} of "
                f"{summary.samples} samples covering {compact(summary.duration_s, 1)}s, "
                "not from one window, so a single spike does not condemn the "
                "machine and a single calm window does not exonerate it. Compare "
                "this to a live get_health call, which sees one window only."
            ),
        }

        if not summary.psi_available:
            result["caveat"] = (
                "At least one sample came from a kernel without PSI, so this "
                "history does not measure saturation. UNKNOWN is not HEALTHY."
            )
        return result

    # ------------------------------------------------------------ verdict --

    def get_node_verdict(node: str, max_samples: int = MAX_SAMPLES) -> dict[str, Any]:
        meta, _, summary = _history(node, max_samples)
        verdict = evaluate(summary, current_type=meta["instance_type"], catalog=cat, thresholds=t)
        payload = _verdict_payload(node, verdict)
        payload["state"] = summary.state.value
        payload["samples"] = summary.samples
        return payload

    # -------------------------------------------------------------- fleet --

    def get_fleet_summary() -> dict[str, Any]:
        rows = store.nodes(ONLINE_WINDOW_S)
        by_state = {state.value: 0 for state in State}
        by_sizing = {sizing.value: 0 for sizing in Sizing}
        monthly = 0.0
        attention: list[dict[str, Any]] = []
        skipped: list[str] = []

        for row in rows:
            raw = store.samples(row["node"], limit=MAX_SAMPLES)
            if not raw:
                skipped.append(row["node"])
                continue

            summary = summarise(
                [Interval.from_dict(r) for r in raw], thresholds=t, label=row["node"]
            )
            verdict = evaluate(
                summary, current_type=row["instance_type"], catalog=cat, thresholds=t
            )

            by_state[summary.state.value] += 1
            by_sizing[verdict.sizing.value] += 1
            monthly += verdict.monthly_delta_usd

            if summary.state.rank > State.HEALTHY.rank:
                worst = summary.resources.get(summary.bottleneck)
                attention.append(
                    {
                        "node": row["node"],
                        "state": summary.state.value,
                        "bottleneck": summary.bottleneck,
                        "online": row["online"],
                        "some_p95_pct": compact(worst.some_p95) if worst else 0.0,
                        "headline": verdict.headline,
                    }
                )

        attention.sort(key=lambda n: n["some_p95_pct"], reverse=True)

        return {
            "nodes": len(rows),
            "online": sum(1 for r in rows if r["online"]),
            "by_state": by_state,
            "by_sizing": by_sizing,
            "monthly_delta_usd": compact(monthly),
            "annual_delta_usd": compact(monthly * 12),
            "needs_attention": attention,
            "no_samples": skipped,
            "note": (
                "monthly_delta_usd is what the fleet would cost if every "
                "recommendation were applied: negative means the fleet is "
                "over-provisioned. needs_attention is every node above HEALTHY, "
                "worst stall first."
            ),
        }

    definitions = (
        (
            list_nodes,
            "list_nodes",
            "Every machine the fleet server has heard from, with its instance "
            "type and whether it is still pushing telemetry. Start here when a "
            "question is about the fleet rather than about this machine — the "
            "node names it returns are what the other fleet tools take.",
        ),
        (
            get_node_health,
            "get_node_health",
            "Saturation history for one node, folded into percentiles across "
            "its stored samples: per-resource state, stall p50/p95/max, "
            "utilisation, reclaim counters, and OOM kills. Much stronger "
            "evidence than a single live window, because a p95 over minutes of "
            "history cannot be fooled by one calm or one unlucky sample. This "
            "is the right tool for 'what is wrong with this instance'.",
        ),
        (
            get_node_verdict,
            "get_node_verdict",
            "What size one node should be, with the reasoning kept separate "
            "from the conclusion: `reasons` is why the engine decided, "
            "`evidence` is the measurements behind it, `caveats` is what would "
            "undermine it. Returns INSUFFICIENT_DATA rather than guessing when "
            "the history is too thin — treat that as an answer, not a failure.",
        ),
        (
            get_fleet_summary,
            "get_fleet_summary",
            "Fleet-wide roll-up: how many nodes are in each health state, how "
            "many are over- or under-sized, the total monthly cost delta if "
            "every recommendation were applied, and every node above HEALTHY "
            "ranked worst-first. Use it to find which node to investigate.",
        ),
    )

    return {
        name: Tool(name=name, tier=Tier.READ_ONLY, description=description, handler=fn)
        for fn, name, description in definitions
    }
