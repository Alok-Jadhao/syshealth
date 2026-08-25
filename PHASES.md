# Phases

SysHealth started as a tool that reports whether a machine is saturated. It is
now a system that detects, investigates, diagnoses, remediates and verifies
incidents inside stated safety boundaries.

This file records what was actually built, what each phase decided and why,
the bugs found along the way, and what is still open. It is written to be
checkable: every claim has a test or a command behind it, and where something
is weaker than it looks, it says so.

| Phase | What it adds | Status |
| ----- | ------------ | ------ |
| 0 | Saturation measurement, right-sizing, fleet telemetry | **done** |
| 1 | MCP foundation — read-only tools for one machine | **done** |
| 2 | SysHealth integration — tools over fleet telemetry | **done** |
| 3 | AI investigation — an agent answers "what is wrong here?" | **done** |
| 4 | Diagnosis — evidence-backed root-cause hypotheses | **done** |
| 5 | Safe remediation — a small catalogue of write actions | **done** |
| 6 | Verification — did remediation actually resolve it? | **done** |
| 7 | Human approval — gates for higher-risk operations | **done** |
| 8 | Autonomous mode — low-risk incidents resolved unattended | **done** |
| 9 | Chaos environment — failure injection, end-to-end evaluation | **done** |

**251 tests**, passing without a PSI kernel, without the MCP SDK, and without
an API key. `make test`, `make lint`, `make mcp-smoke`, `python
tools/sre_smoke.py`.

---

## Phase 0 — the measurement foundation

Not part of the SRE work, but everything rests on it.

- Saturation is measured from the PSI `total=` microsecond counter, not
  `avg10`, so a stall figure is an exact accounting of lost time rather than a
  decaying average that cannot be aggregated.
- Every kernel reader takes a root, which is what makes the stack testable on
  a laptop and on kernels with no PSI.
- Thresholds live in one place, `Thresholds` in `analysis.py`.
- Verdicts carry the evidence they were reached from, and the engine returns
  `INSUFFICIENT_DATA` rather than guessing.

That last property is why this was a plausible base for an autonomous SRE and
a chat wrapper is not: the evidence discipline already existed in code, so the
AI work inherited it rather than having to invent it. See `MIGRATION.md`.

---

## Phase 1 — MCP foundation

Four read-only tools exposing the measurement stack over MCP on stdio:
`get_health`, `get_memory_pressure`, `get_cpu_pressure`,
`get_reclaim_activity`.

**No existing module was modified.** `procfs` → `sampler` → `analysis` was
already a pipeline of pure functions over a configurable root; the tool layer
sits on that seam.

**`tools.py` imports no MCP SDK.** Tools are plain callables returning
JSON-able dicts, so they stay testable where the SDK is absent and can be
called in-process by the agent with no socket in between. The core keeps its
zero runtime dependencies.

**Every tool carries a permission tier from day one** — `READ_ONLY`,
`LOW_RISK`, `HIGH_RISK` — from which MCP's `readOnlyHint` and
`destructiveHint` are derived. It did nothing in Phase 1. It is now the input
to the entire policy engine, which is the argument for having put it there
before it was needed.

**Every metric tool reports saturation and utilisation together.** A tool
schema is a prompt. Handing a model `memory: 94%` invites it to diagnose a
memory problem on a healthy cache-heavy box.

### Two bugs found by driving the server as a real client

**The tool manufactured reassurance.** `get_memory_pressure` attached "this is
the normal reading for a healthy machine" to a window of the thrashing run
with 465 direct reclaims and 33 swap-ins in it. Now gated on a quiet kernel as
well as quiet PSI, scoped to the window it describes, and asserted across
every window of both fixtures.

**Rejections lost their explanation.** The SDK withholds the text of an
unexpected exception, so a bad argument came back as `Error executing tool` with
no bound to retry against. Anticipated rejections now travel as `ToolError`.

---

## Phase 2 — SysHealth integration

Four more read-only tools over the telemetry agents already push:
`list_nodes`, `get_node_health`, `get_node_verdict`, `get_fleet_summary`.

**History, not a window.** A single two-second window is the weakest signal the
project produces — the Phase 1 tools reported `bottleneck: cpu` on the
thrashing fixture because one window happened to look that way.
`get_node_health` folds a node's stored samples through the same `summarise`
the CLI uses, so state comes from a p95 over minutes.

**Read-only is structural.** `--db` opens the database with SQLite's `mode=ro`.
A connection that rejects writes is stronger than a promise about which methods
get called, and it turns a mistyped path into an error rather than a new empty
database reporting an empty fleet.

`get_node_verdict` already returned the Phase 4 shape, because the right-sizing
engine has always kept `reasons`, `evidence` and `caveats` apart from its
conclusion.

---

## Phases 3 & 4 — investigation and diagnosis

An incident is investigated by a **reasoner**: it gathers evidence through the
read-only tools and returns a `Diagnosis`.

```
cause         one sentence
confidence    LOW | MEDIUM | HIGH
observations  facts that were measured
hypotheses    inferences drawn from them — explicitly not the same thing
cites         evidence row ids
recommended   an action from the catalogue, or None
```

### The citation contract

**Every claim must cite evidence that was actually retrieved.** `cites` holds
evidence row ids written by the tool layer at the moment each call was made. A
citation that does not resolve is a *rejected* diagnosis, not a low-confidence
one: it is not recorded, and the incident escalates to a human with the reason.

This is the mechanism behind "the AI must not invent a root cause". It is a
foreign key, not an instruction. `test_a_diagnosis_citing_evidence_that_does_
not_exist_is_rejected` is the test that holds it.

### Two reasoners, one contract

`RuleReasoner` is the **default** and needs no API key. It is not a placeholder:
for the failure modes this project measures, the rules encode what the evidence
means using the same thresholds as the verdict engine, and they are
inspectable, reproducible, and unable to hallucinate. Their honest limitation
is coverage, and they say so plainly rather than answering confidently about a
pattern they do not recognise.

`ClaudeReasoner` calls the Claude API with the same tools attached, for
situations the rules do not cover. A manual tool-use loop rather than the SDK's
tool runner, because every call has to be recorded as evidence at the moment it
happens — the audit trail is the product, not a side effect.

**The reasoner is not trusted by anything downstream.** Its output selects an
action; whether that action may run is decided entirely by the policy engine,
which is never told what the reasoner believes.

---

## Phase 5 — safe remediation

Five actions, in one catalogue, with tiers and typed argument schemas:
`restart_service`, `restart_container`, `drop_page_cache`, `clear_temp_files`
(LOW_RISK) and `terminate_instance` (HIGH_RISK).

**There is no shell action and no argument that reaches a subprocess as free
text.** Execution is a dictionary lookup into the catalogue followed by a call
to a Python function with validated arguments. There is no code path from a
model's output to an argument vector. Two tests enforce this — one asserting
the catalogue contains no execution primitive, one parsing `executor.py`'s AST
to prove no call passes `shell=True` or reaches `os.system`/`eval`/`exec`.

**Targets are declared in advance.** A restart aimed at a name a reasoner
inferred is worse than proposing nothing: it either fails or restarts the
wrong thing. `--managed web-01=service:app` is configuration. A node with
nothing declared is investigated and diagnosed but never restarted.

**The remediation channel is poll-based.** This was the open architectural
decision at the end of Phase 2, and the answer is the option recommended
there. Nodes poll for approved work and open no port; the previous agent's
unauthenticated `POST /run-stress` is not being reopened with a password on
it. Every action is a durable audited row before any node can see it, and the
claim is an `UPDATE`, so two pollers racing cannot both receive the same work.

`terminate_instance` is deliberately **not implemented on the node**. A machine
must not be able to destroy itself on instruction from a poll response;
instance lifecycle belongs in the control plane with its own credentials.

---

## Phase 6 — verification

**A command that succeeded is not an incident that resolved.** `restart_service`
returning exit 0 moves an incident to `VERIFYING`, never to `RESOLVED`.

Verification re-measures the machine and checks five things: it is measurable
at all, the node is still reporting, the health state improved, the stall that
caused the incident actually fell, and the kernel is no longer under duress
(no OOM kills or swap-in since). A node that went silent after a restart fails
the second check — that is a worse outcome than the incident, not a fixed one.

`verify_recovery` is a pure function over two `get_node_health` payloads, so it
is testable against recorded data and the loop owns when the reading is taken.
There is a settle delay before measuring, because a machine sampled the instant
a service restarts is not the machine you will have in thirty seconds.

---

## Phase 7 — human approval

`AWAITING_APPROVAL` actions surface at `GET /approvals`, on the dashboard, and
in `syshealth incidents`, each with the reason, the policy ruling, and the
action's declared blast radius — because someone is about to make a decision
on that basis.

- An action can be decided **once**. A second approval cannot resurrect a
  rejected one.
- An unanswered approval **expires**. The machine's state has moved on, and a
  stale yes must not execute against it; the action is re-proposed instead.
- Policy guards are checked **before** the approval path, so a human is never
  asked to approve the fourth restart in five minutes — the guard refused it
  before they saw it.
- Only a `DISPATCHED` action can report a result, so nothing that can reach the
  API can mark work done.

---

## Phase 8 — autonomous mode

Three modes, defaulting to the one that cannot change anything.

| Mode | May do |
| ---- | ------ |
| `OBSERVE` | investigate and recommend. **Default.** |
| `ASSIST` | propose actions; every change needs a human |
| `AUTONOMOUS` | run *listed* low-risk actions unattended |

**HIGH_RISK requires a human in every mode, including AUTONOMOUS.** That is the
property that must never regress, and it has its own test.

Turning autonomy on and choosing what it may do are two separate decisions: an
action absent from `--autonomous-actions` degrades to asking a human rather
than being refused, so tightening the list cannot silently break incident
response.

### Why the loop terminates

The dangerous failure of an autonomous system is not one wrong action; it is
the same wrong action, forever, faster than anyone can intervene. Four guards:

- **attempt limit** (2) — every unsuccessful outcome passes through a counter
  the policy engine bounds. There is no edge back to remediation that skips it.
- **cooldown** (300s, per node and action) — two incidents on correlated
  symptoms cannot become a restart loop.
- **blast radius** (1 node) — a fleet-wide symptom cannot become a fleet-wide
  action.
- **incident timeout** (1800s) — an incident open too long escalates.

`run_until_settled` raises if incidents are still moving after its bound, so a
guard that stops terminating is a loud test failure rather than a spin.

**Confidence cannot widen permissions.** `Policy.rule` has no confidence
parameter, and a test asserts its exact signature. "High confidence" is not an
argument the policy engine accepts, because the premise is that a generated
diagnosis may be wrong.

---

## Phase 9 — chaos environment

A memory-limited container running a deliberately faulty service, plus
`chaos/demo.sh` to inject failures and run the loop against them. Details in
`chaos/README.md`.

Per-container measurement needed a new capability: `/proc/pressure` is
host-wide, so `cgroup.py` reads the same PSI counters from cgroup v2 per
control group. It is interface-compatible with `ProcReader`, so `Sampler`,
`diff`, `summarise` and every tool consume it unchanged.

### Two bugs it caught that the unit tests could not

**Incidents were titled by the wrong resource.** A container stalling 2.9% on
memory and 52% on io was reported as a memory incident, because the detector
returned the first resource crossing a threshold rather than the worst. The
investigation then contradicted the incident it was opened for.

**Saturation via full-stall was missed.** The reasoner tested only the `some`
share, so a container whose full-stall reached 3% — windows in which *nothing
ran* — fell through to "no fault found".

Both are correctness bugs in the reasoning path, both needed a real kernel
under real pressure to surface, and both are now regression-tested. That is
the argument for the environment existing.

After the fixes, against a real thrashing container, the diagnosis reads:

> Without OOM kills or swap-in, this is more consistent with heavy page-cache
> churn than with a hard leak — a workload reading more than fits, rather than
> a process growing. **Restarting a container would not fix cache churn.**

Which is exactly what the scenario does.

---

## Known gaps

Things that are true right now and should not be discovered later by surprise.

- **The fleet server has no authentication.** This mattered less when the
  server only collected metrics. It now approves actions that change machines,
  and `POST /actions/<id>/decision` is unauthenticated. Bind it to a private
  interface and put a reverse proxy in front, and treat real auth as the next
  piece of work rather than a refinement.
- **`--reasoner claude` has not been run against the live API here.** Its
  contract is unit-tested and its parsing is exercised, but no call has been
  made with a real key in this repository. Treat the first live run as an
  experiment.
- **`clear_temp_files` and `drop_page_cache` have no diagnosis that proposes
  them.** They exist in the catalogue and execute correctly; no reasoner rule
  currently selects them, because the project measures memory saturation and
  neither is the right response to it.
- **Detection covers saturation, not application symptoms.** Latency, error
  rate, and connection-pool exhaustion are in the brief's example diagnosis and
  are not measured. That needs an application-metrics source, not another rule.
- **One reasoner, one node at a time.** The blast-radius guard is set to 1 and
  the loop advances incidents sequentially. Fine at fleet sizes where a human
  reads every incident; not yet a design for hundreds of nodes.
- **`get_node_health` has no notion of *when*.** Percentiles describe a window;
  they cannot say a problem started twelve minutes ago, which is the first
  question in a real incident. A bucketed trend tool is the obvious next
  addition.
- **Verification thresholds are heuristics.** "Half the original stall, or
  below the degraded threshold" is reasonable and is not derived from anything.
- **Nothing here has been run against production.** Every claim is backed by
  recorded runs, unit tests, and one deliberately broken container. That is the
  right place to start and it is not the same as evidence from a real fleet.

## If you deploy this

In order: run `OBSERVE` for long enough to trust the diagnoses; move to
`ASSIST` and read every proposal before approving it; only then enable
`AUTONOMOUS`, for one action, on one node, with the cooldown long. The defaults
are built so that forgetting a step fails safe.
