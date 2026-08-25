"""Turning evidence into a diagnosis.

A ``Reasoner`` takes an incident and a set of read-only tools, gathers
evidence, and returns a ``Diagnosis``. It is one component behind an
interface, and that is the point: the safety machinery — policy, catalogue,
audit, verification — does not depend on which reasoner produced the
hypothesis, so a wrong or hallucinated diagnosis cannot widen what the system
is permitted to do.

Two implementations ship.

``RuleReasoner`` is the default and needs no API key. For the failure modes
this project actually measures, the rules encode what the evidence means, and
they do it better than a language model would: the thresholds are the same
ones the verdict engine uses, the reasoning is inspectable, and it produces
identical output for identical input, which is what makes it testable in CI.

``ClaudeReasoner`` calls the Claude API with the tools attached. It is the
right choice for open-ended situations the rules do not cover — an unfamiliar
combination of symptoms, or evidence that needs a judgement call. It is
strictly more capable and strictly less predictable, which is why it is opt-in
rather than the default.

Both are held to the same contract, enforced in ``Diagnosis.validate``:

**Every claim cites evidence that was actually retrieved.** ``cites`` holds
evidence row ids. A citation that does not resolve is a failed diagnosis, not
a low-confidence one. This is the mechanism behind "the AI must not invent a
root cause" — it is checked, not requested.

**Observations and hypotheses are separate fields.** A measurement and an
inference from it are different kinds of claim, and collapsing them is how a
guess gets laundered into a fact.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..analysis import State, Thresholds
from ..mcp.tools import Tool
from .actions import Action, build

CONFIDENCE = ("LOW", "MEDIUM", "HIGH")


class DiagnosisError(RuntimeError):
    """A reasoner produced something that does not meet the contract."""


@dataclass
class Diagnosis:
    """A probable cause, with the reasoning kept apart from the conclusion."""

    cause: str
    confidence: str
    reasoner: str

    observations: list[str] = field(default_factory=list)
    """Measured facts. Each must be traceable to a retrieved tool result."""

    hypotheses: list[str] = field(default_factory=list)
    """Inferences drawn from those facts. Explicitly not the same thing."""

    cites: list[int] = field(default_factory=list)
    """Evidence row ids. Must resolve, or the diagnosis is rejected."""

    recommended: Action | None = None
    """Chosen from the catalogue. None means "nothing here should be done"."""

    def validate(self, available: set[int]) -> None:
        if self.confidence not in CONFIDENCE:
            raise DiagnosisError(
                f"confidence must be one of {', '.join(CONFIDENCE)}; got {self.confidence!r}"
            )
        if not self.cause.strip():
            raise DiagnosisError("a diagnosis must name a cause")
        if not self.cites:
            raise DiagnosisError(
                "a diagnosis must cite the evidence it was reached from. "
                "An uncited conclusion is a guess, and this system does not "
                "record guesses as diagnoses."
            )
        invented = sorted(set(self.cites) - available)
        if invented:
            raise DiagnosisError(
                f"cites evidence that was never collected: {invented}. "
                f"Available evidence ids: {sorted(available)}"
            )
        if not self.observations:
            raise DiagnosisError("a diagnosis must state the facts it observed")

    def to_dict(self) -> dict[str, Any]:
        return {
            "cause": self.cause,
            "confidence": self.confidence,
            "reasoner": self.reasoner,
            "observations": self.observations,
            "hypotheses": self.hypotheses,
            "cites": self.cites,
            "recommended": self.recommended.describe() if self.recommended else None,
        }


class Reasoner(Protocol):
    name: str

    def investigate(self, context: InvestigationContext) -> Diagnosis:
        """Gather evidence through ``context`` and return a diagnosis."""
        ...


@dataclass
class InvestigationContext:
    """What a reasoner may do, and the record of what it did.

    Every tool call goes through ``call``, which records the result as
    evidence before returning it. A reasoner cannot see a measurement that was
    not written down, which is what makes the citation check meaningful.
    """

    incident_id: str
    node: str
    title: str
    tools: dict[str, Tool]
    record: Any
    """The IncidentStore. Named generically to keep this module importable
    without it in tests."""

    managed: dict[str, str] = field(default_factory=dict)
    """What this node is declared to run, e.g. ``{"service": "app"}``.

    A remediation target must be declared in advance, in configuration, not
    discovered by a reasoner at diagnosis time. Restarting a service whose
    name was inferred is how the wrong thing gets restarted; an empty mapping
    means nothing on this node may be restarted, and the reasoner must decline
    rather than guess.
    """

    max_calls: int = 12
    """A bounded investigation. An unbounded loop against a live fleet is a
    cost and load problem, and a reasoner that needs fifty calls to form a
    hypothesis is not going to form a good one on the fifty-first."""

    _calls: int = 0
    _evidence: dict[int, dict[str, Any]] = field(default_factory=dict)

    def call(self, tool: str, **arguments: Any) -> dict[str, Any]:
        """Invoke a read-only tool and record the result as evidence.

        Returns the tool's payload with ``evidence_id`` and ``ok`` added. A
        failed probe returns ``ok=False`` and an ``error`` rather than raising:
        that something could not be measured is itself evidence, and it is
        recorded as such.
        """
        if self._calls >= self.max_calls:
            raise DiagnosisError(
                f"investigation exceeded {self.max_calls} tool calls without "
                "reaching a conclusion"
            )
        self._calls += 1

        spec = self.tools.get(tool)
        if spec is None:
            available = ", ".join(sorted(self.tools))
            raise DiagnosisError(f"unknown tool {tool!r}. Available: {available}")

        try:
            result = spec.handler(**arguments)
            ok = True
        except Exception as exc:  # recorded, not swallowed: a failed probe is evidence
            result = {"error": str(exc)}
            ok = False

        evidence_id = self.record.add_evidence(
            self.incident_id, tool, arguments, result, ok=ok
        )
        self._evidence[evidence_id] = {"tool": tool, "arguments": arguments, "result": result}
        return {**result, "evidence_id": evidence_id, "ok": ok}

    @property
    def collected(self) -> set[int]:
        return set(self._evidence)

    def latest(self, tool: str) -> tuple[int, dict[str, Any]] | None:
        """The most recent successful result from one tool, with its id."""
        for evidence_id in sorted(self._evidence, reverse=True):
            entry = self._evidence[evidence_id]
            if entry["tool"] == tool and "error" not in entry["result"]:
                return evidence_id, entry["result"]
        return None


# ----------------------------------------------------------- rule reasoner --


class RuleReasoner:
    """Deterministic diagnosis from the measurements this project understands.

    Not a placeholder for a "real" reasoner. For memory saturation — the
    failure this whole codebase is built to detect — the rules below encode
    what the evidence means, using the same thresholds as the verdict engine.
    They are inspectable, reproducible, and cannot hallucinate.

    The honest limitation is coverage: they know the failures they were
    written for and say so plainly when the evidence does not match one. That
    is the correct behaviour — a confident answer to an unrecognised pattern
    would be worse than an admission.
    """

    name = "rules"

    def __init__(self, thresholds: Thresholds | None = None) -> None:
        self.t = thresholds or Thresholds()

    def investigate(self, context: InvestigationContext) -> Diagnosis:
        health_id, health = self._health(context)
        cites = [health_id]

        state = State(health.get("state", State.UNKNOWN.value))
        resources = health.get("resources", {})
        memory = resources.get("memory", {})
        reclaim = health.get("reclaim", {})
        utilisation = health.get("utilisation", {})

        memory_p95 = float(memory.get("some_p95_pct", 0.0))
        memory_full = float(memory.get("full_max_pct", 0.0))
        oom = int(reclaim.get("oom_kills", 0))
        swap_in = int(reclaim.get("swap_in_total", 0))
        direct_per_s = float(reclaim.get("direct_reclaim_per_s", 0.0))
        naive = float(utilisation.get("naive_used_pct_max", 0.0))

        observations = [
            f"health state is {state.value} over {health.get('window', {}).get('samples', 0)} "
            f"samples ({health.get('window', {}).get('duration_s', 0)}s of history)",
            f"memory stall p95 {memory_p95:.2f}%, full-stall max {memory_full:.2f}%",
            f"direct reclaim {direct_per_s:.0f} pages/s, swap-in {swap_in}, OOM kills {oom}",
            f"memory utilisation peaked at {naive:.1f}% by the used/free rule",
        ]

        # PSI unavailable: nothing was measured, so nothing may be concluded.
        if not health.get("psi_available", True):
            return Diagnosis(
                cause="cannot be determined: this node's kernel does not expose PSI",
                confidence="LOW",
                reasoner=self.name,
                observations=observations,
                hypotheses=[
                    "Saturation was not measured, so the health state is UNKNOWN "
                    "rather than healthy. No remediation can be justified from "
                    "evidence that does not exist."
                ],
                cites=cites,
                recommended=None,
            )

        # The unambiguous case: the kernel killed something, or is swapping.
        if oom > 0 or (memory_p95 >= self.t.some_saturated and swap_in > 0):
            verdict_id, verdict = self._verdict(context)
            if verdict_id:
                cites.append(verdict_id)
                observations.append(
                    f"sizing verdict: {verdict.get('sizing')} "
                    f"({verdict.get('confidence')}) — {verdict.get('headline', '')}"
                )
            return Diagnosis(
                cause="memory exhaustion: the working set does not fit in RAM",
                confidence="HIGH",
                reasoner=self.name,
                observations=observations,
                hypotheses=[
                    f"Tasks were blocked waiting for memory {memory_p95:.2f}% of "
                    "wall-clock time at p95, and the kernel had to reclaim "
                    "synchronously rather than in the background.",
                    (
                        f"{oom} OOM kill(s) occurred — the kernel chose to kill a "
                        "process rather than fail an allocation."
                        if oom
                        else f"{swap_in} pages were swapped back in, so evicted "
                        "pages were still wanted."
                    ),
                    "A restart frees the leaked memory but does not change the "
                    "working set. If this recurs after remediation, the machine "
                    "is undersized rather than leaking.",
                ],
                cites=cites,
                recommended=self._restart_or_none(context),
            )

        # Saturated on memory without OOM or swap: real, but less certain why.
        # Tested the same way ``summarise`` decides SATURATED — on either
        # share. A container whose full-stall hits 3% had windows in which
        # *nothing ran*, and reading only ``some`` would let that fall through
        # to the generic branch and be reported as no fault found.
        if state is State.SATURATED and self._memory_saturated(memory_p95, memory_full):
            return Diagnosis(
                cause="memory saturation: sustained stalling without OOM or swap-in",
                confidence="MEDIUM",
                reasoner=self.name,
                observations=observations,
                hypotheses=[
                    f"Memory stall p95 was {memory_p95:.2f}% and full-stall "
                    f"reached {memory_full:.2f}% — windows in which no task on "
                    "the machine made progress at all.",
                    f"Direct reclaim ran at {direct_per_s:.0f} pages/s, so "
                    "allocations were freeing memory synchronously.",
                    "Without OOM kills or swap-in, this is more consistent with "
                    "heavy page-cache churn than with a hard leak — a workload "
                    "reading more than fits, rather than a process growing.",
                    "Restarting a container would not fix cache churn. Confirm "
                    "which process is responsible before acting.",
                ],
                cites=cites,
                recommended=None,
            )

        # High utilisation, no stalling: the false alarm this project exists for.
        if naive >= 90.0 and memory_p95 < self.t.some_degraded and direct_per_s < self.t.quiet_reclaim_per_s:
            return Diagnosis(
                cause="no fault: high memory utilisation with no saturation",
                confidence="HIGH",
                reasoner=self.name,
                observations=observations,
                hypotheses=[
                    f"Utilisation peaked at {naive:.1f}% while memory stalled "
                    f"only {memory_p95:.2f}% of wall-clock time, with no "
                    "meaningful reclaim.",
                    "Linux fills otherwise-idle RAM with page cache, which the "
                    "used/free rule counts as used. This is the normal reading "
                    "for a healthy machine.",
                    "Remediating this would cost an outage to fix nothing.",
                ],
                cites=cites,
                recommended=None,
            )

        # Another resource is the bottleneck.
        bottleneck = health.get("bottleneck")
        if bottleneck and bottleneck != "memory":
            resource = resources.get(bottleneck, {})
            return Diagnosis(
                cause=f"{bottleneck} saturation",
                confidence="MEDIUM",
                reasoner=self.name,
                observations=observations
                + [
                    f"{bottleneck} stall p95 {resource.get('some_p95_pct', 0):.2f}%, "
                    f"max {resource.get('some_max_pct', 0):.2f}%"
                ],
                hypotheses=[
                    f"The bottleneck is {bottleneck}, not memory.",
                    "This reasoner has no remediation rule for that resource, so "
                    "it proposes none rather than guessing at one.",
                ],
                cites=cites,
                recommended=None,
            )

        return Diagnosis(
            cause="no fault found: the node is within thresholds",
            confidence="MEDIUM",
            reasoner=self.name,
            observations=observations,
            hypotheses=[
                "No resource is stalling above its threshold over the observed "
                "history. If the incident was opened on a transient spike, it "
                "has passed."
            ],
            cites=cites,
            recommended=None,
        )

    # -- evidence gathering -------------------------------------------------

    def _memory_saturated(self, some_p95: float, full_max: float) -> bool:
        """The same test ``summarise`` applies. Kept in one expression so the
        reasoner and the classifier cannot drift apart."""
        return some_p95 >= self.t.some_saturated or full_max >= self.t.full_saturated

    def _health(self, context: InvestigationContext) -> tuple[int, dict[str, Any]]:
        if "get_node_health" in context.tools:
            result = context.call("get_node_health", node=context.node)
        else:
            result = context.call("get_health")
        if not result.get("ok", True):
            raise DiagnosisError(f"could not read health for {context.node}: {result.get('error')}")
        return result["evidence_id"], result

    def _verdict(self, context: InvestigationContext) -> tuple[int | None, dict[str, Any]]:
        if "get_node_verdict" not in context.tools:
            return None, {}
        result = context.call("get_node_verdict", node=context.node)
        if not result.get("ok", True):
            return None, {}
        return result["evidence_id"], result

    def _restart_or_none(self, context: InvestigationContext) -> Action | None:
        """Propose a restart only against a target declared in configuration.

        A restart aimed at a guessed name is worse than proposing nothing: it
        either fails, or restarts something nobody meant to touch. So the
        target comes from ``managed``, which an operator sets in advance. With
        nothing declared, this declines — and the incident escalates to a human
        with the diagnosis attached, which is the right outcome.
        """
        if service := context.managed.get("service"):
            return build("restart_service", {"service": service})
        if container := context.managed.get("container"):
            return build("restart_container", {"container": container})
        return None


# --------------------------------------------------------- Claude reasoner --

SYSTEM_PROMPT = """\
You are an SRE investigating one incident on one Linux machine. You have
read-only measurement tools. You cannot change anything.

SysHealth measures saturation — the share of wall-clock time tasks spent
unable to make progress, from kernel PSI — not utilisation. These are
different and the difference matters: Linux fills idle RAM with page cache, so
a healthy machine routinely reads 90%+ memory "used" while stalling zero.
Diagnose from stall percentages. Treat utilisation as context.

Investigate, then produce a diagnosis. Rules you must follow:

- Every observation you state must come from a tool result you actually
  retrieved in this investigation. Do not state a number you did not measure.
- Cite the `evidence_id` of every tool result you relied on.
- Keep observations (what you measured) separate from hypotheses (what you
  infer from it). Do not present an inference as a measurement.
- If the evidence does not support a confident cause, say so and use LOW
  confidence. An honest "insufficient evidence" is a correct answer.
- Recommend an action only from the catalogue you are given, and only if the
  evidence supports it. Recommending nothing is usually right.
- High utilisation with no stalling is not a fault. Do not recommend action
  for it.

When you are done investigating, call `submit_diagnosis` exactly once."""


class ClaudeReasoner:
    """Diagnosis via the Claude API, with the read-only tools attached.

    A manual tool-use loop rather than the SDK's tool runner, because every
    call has to be recorded as evidence at the moment it happens — the audit
    trail is the product here, not a side effect — and the loop has to be
    bounded by the same ``max_calls`` the rule reasoner respects.

    The model's output is not trusted on the way out. It is parsed into a
    ``Diagnosis`` and put through the same ``validate`` as everything else, so
    a fabricated citation fails here rather than reaching the audit log.
    """

    name = "claude"

    def __init__(
        self,
        model: str = "claude-opus-5",
        client: Any = None,
        max_tokens: int = 8000,
    ) -> None:
        self.model = model
        self.max_tokens = max_tokens
        self._client = client

    @property
    def client(self):
        if self._client is None:
            try:
                import anthropic
            except ImportError as exc:  # pragma: no cover
                raise DiagnosisError(
                    "the Claude reasoner needs the SDK: pip install 'syshealth[ai]'"
                ) from exc
            if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
                raise DiagnosisError(
                    "no Claude credentials found. Set ANTHROPIC_API_KEY, or run "
                    "'ant auth login', or use the default rule-based reasoner "
                    "(--reasoner rules), which needs no API access."
                )
            self._client = anthropic.Anthropic()
        return self._client

    def investigate(self, context: InvestigationContext) -> Diagnosis:
        tools = self._tool_schemas(context)
        messages: list[dict[str, Any]] = [
            {"role": "user", "content": self._opening(context)}
        ]

        for _ in range(context.max_calls + 2):
            response = self.client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                system=SYSTEM_PROMPT,
                thinking={"type": "adaptive"},
                tools=tools,
                messages=messages,
            )

            if response.stop_reason == "refusal":
                raise DiagnosisError("the model declined to answer this request")

            messages.append({"role": "assistant", "content": response.content})

            calls = [b for b in response.content if b.type == "tool_use"]
            if not calls:
                raise DiagnosisError(
                    "the model stopped without submitting a diagnosis"
                )

            results = []
            for call in calls:
                if call.name == "submit_diagnosis":
                    return self._parse(call.input, context)
                results.append(self._run(call, context))

            messages.append({"role": "user", "content": results})

        raise DiagnosisError(
            f"investigation did not converge within {context.max_calls} tool calls"
        )

    # -- plumbing -----------------------------------------------------------

    def _opening(self, context: InvestigationContext) -> str:
        from .actions import catalogue

        return (
            f"Incident {context.incident_id} on node {context.node}.\n"
            f"Symptom that opened it: {context.title}\n\n"
            "Actions available for remediation (you may recommend at most one, "
            "or none):\n"
            f"{json.dumps(catalogue(), indent=2)}\n\n"
            "Investigate and then call submit_diagnosis."
        )

    def _tool_schemas(self, context: InvestigationContext) -> list[dict[str, Any]]:
        schemas: list[dict[str, Any]] = []
        for name, tool in sorted(context.tools.items()):
            schemas.append(
                {
                    "name": name,
                    "description": tool.description,
                    "input_schema": _schema_for(tool),
                }
            )
        schemas.append(
            {
                "name": "submit_diagnosis",
                "description": (
                    "Submit your final diagnosis. Call this exactly once, when "
                    "you have gathered enough evidence."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "cause": {
                            "type": "string",
                            "description": "The probable root cause, in one sentence.",
                        },
                        "confidence": {"type": "string", "enum": list(CONFIDENCE)},
                        "observations": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Facts you measured. No inferences here.",
                        },
                        "hypotheses": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "What you infer from those facts.",
                        },
                        "cites": {
                            "type": "array",
                            "items": {"type": "integer"},
                            "description": (
                                "evidence_id of every tool result you relied on. "
                                "Must be ids returned to you in this investigation."
                            ),
                        },
                        "recommended_action": {
                            "type": ["string", "null"],
                            "description": (
                                "Action name from the catalogue, or null if "
                                "nothing should be done."
                            ),
                        },
                        "recommended_args": {
                            "type": ["object", "null"],
                            "description": "Arguments for that action.",
                        },
                    },
                    "required": ["cause", "confidence", "observations", "cites"],
                },
            }
        )
        return schemas

    def _run(self, call, context: InvestigationContext) -> dict[str, Any]:
        try:
            result = context.call(call.name, **dict(call.input))
            payload, is_error = json.dumps(result, default=str), False
        except DiagnosisError as exc:
            payload, is_error = str(exc), True
        return {
            "type": "tool_result",
            "tool_use_id": call.id,
            "content": payload,
            "is_error": is_error,
        }

    def _parse(self, raw: dict[str, Any], context: InvestigationContext) -> Diagnosis:
        action = None
        name = raw.get("recommended_action")
        if name:
            # An invented action name fails here, in the catalogue, rather than
            # anywhere near an executor.
            action = build(name, raw.get("recommended_args") or {})

        return Diagnosis(
            cause=str(raw.get("cause", "")),
            confidence=str(raw.get("confidence", "LOW")).upper(),
            reasoner=self.name,
            observations=[str(x) for x in raw.get("observations", [])],
            hypotheses=[str(x) for x in raw.get("hypotheses", [])],
            cites=[int(x) for x in raw.get("cites", [])],
            recommended=action,
        )


def _schema_for(tool: Tool) -> dict[str, Any]:
    """Derive a JSON schema from a tool handler's signature.

    Small and deliberate: the tool layer owns its own validation, so this only
    has to describe the surface well enough for the model to call it.
    """
    import inspect

    properties: dict[str, Any] = {}
    required: list[str] = []
    types = {int: "integer", float: "number", str: "string", bool: "boolean"}

    for name, parameter in inspect.signature(tool.handler).parameters.items():
        properties[name] = {"type": types.get(parameter.annotation, "string")}
        if parameter.default is inspect.Parameter.empty:
            required.append(name)
        else:
            properties[name]["default"] = parameter.default

    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def get_reasoner(name: str, thresholds: Thresholds | None = None) -> Reasoner:
    if name == "rules":
        return RuleReasoner(thresholds)
    if name == "claude":
        return ClaudeReasoner()
    raise ValueError(f"unknown reasoner {name!r}; expected 'rules' or 'claude'")
