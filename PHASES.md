# Phases

The plan is to grow SysHealth from a tool that reports whether a machine is
saturated into one that detects, investigates, fixes and verifies incidents
inside stated safety boundaries. This file records what is actually built,
what each phase decided and why, and what is still open.

It is written to be checkable. Every "done" below has tests and a command you
can run; where something is weaker than it looks, it says so.

| Phase | What it adds | Status |
| ----- | ------------ | ------ |
| 0 | Saturation measurement, right-sizing, fleet telemetry | **done** |
| 1 | MCP foundation — read-only tools for one machine | **done** |
| 2 | SysHealth integration — tools over fleet telemetry | **done** |
| 3 | AI investigation — an agent answers "what is wrong here?" | not started |
| 4 | Diagnosis — evidence-backed root-cause hypotheses | not started |
| 5 | Safe remediation — a very small number of write actions | not started |
| 6 | Verification — did the fix actually work? | not started |
| 7 | Human approval — gates for higher-risk operations | not started |
| 8 | Autonomous mode — low-risk incidents resolved unattended | not started |
| 9 | Chaos environment — failure injection, end-to-end evaluation | not started |

**143 tests**, passing without a PSI kernel and without the MCP SDK. `make
test`, `make lint`, `make mcp-smoke`.

---

## Phase 0 — the measurement foundation

Not part of the SRE work, but everything after it rests here, so it is worth
stating what was already true before any of this started.

- Saturation is measured from the PSI `total=` microsecond counter, not
  `avg10`. A stall figure is therefore an exact accounting of time lost
  between two samples rather than a decaying average that cannot be
  aggregated or compared.
- Every kernel reader takes a `root`, which is what makes the whole stack
  testable on a laptop, in CI, and on kernels with no PSI at all.
- Thresholds live in one place, `Thresholds` in `analysis.py`, so "why did it
  say that?" always has exactly one place to look.
- Verdicts carry the evidence they were reached from, and the engine returns
  `INSUFFICIENT_DATA` rather than guessing when the history is thin.

That last property is the reason this project is a plausible base for an
autonomous SRE and a chat wrapper is not. The evidence discipline already
exists in code; the AI work inherits it instead of having to invent it.

See `MIGRATION.md` for what changed from the original prototype.

---

## Phase 1 — MCP foundation

**Built.** Four read-only tools exposing the measurement stack to an AI client
over MCP, on stdio.

| Tool | Returns |
| ---- | ------- |
| `get_health` | per-resource state and the bottleneck, for one measured window |
| `get_memory_pressure` | memory saturation and both utilisation figures, side by side |
| `get_cpu_pressure` | CPU stall next to CPU busy |
| `get_reclaim_activity` | direct reclaim, major faults, swap, OOM kills |

```bash
pip install -e '.[mcp]'
claude mcp add syshealth -- syshealth mcp
make mcp-smoke      # a real client, both modes, end to end
```

### Decisions

**No existing module was modified to make this fit.** `procfs` → `sampler` →
`analysis` was already a pipeline of pure functions over a configurable root.
The tool layer sits on that seam.

**`tools.py` imports no MCP SDK.** Tools are plain callables returning
JSON-able dicts. This keeps them testable where the SDK is absent — CI runs on
macOS on purpose — and lets a future agent call them in-process with no socket
in between. `server.py` is the only module that touches the SDK, and the SDK
is an optional extra, so the core keeps its zero runtime dependencies.

**Every tool carries a permission tier from day one.** `READ_ONLY`,
`LOW_RISK`, `HIGH_RISK`. Everything is read-only today and the tier does
nothing but become MCP's `readOnlyHint` and `destructiveHint`. It exists now
because a policy layer retrofitted onto tools written without one is how an
agent ends up with authority nobody granted it.

**Every metric tool reports saturation and utilisation together.** A tool
schema is a prompt — it is the only description of the data a model ever
reads. Handing one `memory: 94%` invites it to diagnose a memory problem on a
healthy cache-heavy box, which is the false alarm this project exists to
refute. The descriptions say, in the text the model sees, which number is
evidence and which one lies.

**`--replay` serves a recorded run.** No static `/proc` fixture can produce a
non-zero stall, because PSI's `total=` is a counter and reading the same file
twice yields a delta of zero. Recordings are therefore the only way to
demonstrate a saturated machine without saturating one, and the only way to
exercise the interesting paths on a laptop.

### Two bugs found by driving the server as a real client

Both were invisible to the unit tests, and both are worth remembering because
they are the failure classes this system will keep producing.

**The tool manufactured reassurance.** `get_memory_pressure` attached "this is
the normal reading for a healthy machine" to a window of the *thrashing* run
that had 465 direct reclaims and 33 swap-ins in it. PSI read zero for that
window; the kernel was already in trouble. Gating the note on stall alone was
wrong. It is now gated on a quiet kernel as well, scoped to the window it
describes rather than to the machine, and asserted across *every* window of
both fixtures rather than a sample of them.

**Rejections lost their explanation.** The SDK deliberately withholds the text
of an unexpected exception, so an out-of-range argument came back as `Error
executing tool get_health` with no bound to retry against. Anticipated
rejections now travel as the SDK's `ToolError`; genuine crashes keep the
generic treatment so they cannot leak internals.

---

## Phase 2 — SysHealth integration

**Built.** Four more read-only tools, over the telemetry agents already push
to the fleet server.

| Tool | Returns |
| ---- | ------- |
| `list_nodes` | every machine the server has heard from, and whether it still is |
| `get_node_health` | one node's saturation folded into percentiles across its history |
| `get_node_verdict` | what size it should be, with `reasons`, `evidence` and `caveats` kept apart |
| `get_fleet_summary` | states, sizings, total monthly delta, and what to investigate first |

```bash
syshealth mcp --db fleet.db              # this machine and the fleet
syshealth mcp --db fleet.db --fleet-only # the fleet alone
```

### Decisions

**History, not a window.** This is the point of the phase. A single two-second
window is the weakest signal the project produces: a thrashing box frequently
reads calm for one window, and a healthy one occasionally reads alarming. The
Phase 1 tools demonstrated exactly that — on the thrashing fixture they
reported `bottleneck: cpu` because that one window happened to look that way.
`get_node_health` folds a node's stored samples through the same `summarise`
the CLI uses, so state comes from a p95 over minutes. Nothing re-implements
analysis; these tools compose `summarise` and `evaluate`.

**Read-only is structural, not a convention.** `--db` opens the database with
SQLite's `mode=ro`. The MCP process only ever calls read methods, but a
promise about which methods get called is weaker than a connection that
rejects writes. It also turns a mistyped path into an error instead of a new
empty database silently reporting an empty fleet — the more dangerous of the
two failures.

**The Flask payload was deliberately not reused.** `GET /fleet` returns a
dashboard row: headline, sizing, delta. An agent needs the evidence, not the
headline. Extracting a shared helper would have forced one shape onto two
genuinely different consumers, so the two compose the same functions
independently.

**Unknown inputs fail with something actionable.** An unknown node name comes
back with the list of node names that do exist, so a caller that guessed wrong
can recover in one step instead of guessing again.

### What this phase makes possible

`get_node_verdict` already returns the Phase 4 shape, because the right-sizing
engine has always separated its reasoning from its conclusion:

```
sizing      UNDERSIZED          confidence  HIGH
reasons     why the engine decided
evidence    the measurements it decided from
caveats     what would undermine it
```

That is §6's "observed fact / hypothesis / recommended action" split, computed
rather than generated. Phase 4 should extend this structure rather than invent
a parallel one, and an AI diagnosis should be required to cite it.

---

## Phase 3 — AI investigation *(next)*

Let an agent answer "what is wrong with this instance?" using the tools.

Scope: a single agent, the existing MCP tools, no new capabilities. The output
is a written investigation citing tool results, not an action.

Open questions to settle first:

- **Where the loop runs.** In-process against the tool registry (no socket,
  easy to test) or as an MCP client against the server (proves the same path a
  human client uses). The registry was built to support both.
- **What a trend tool should look like.** Percentiles say how bad; they do not
  say *when it started*, which is the first question in any real incident. A
  bucketed `get_node_trend` is probably the missing tool, and it should land
  before the agent rather than after, or the agent will learn to work without
  it.
- **How to stop invented citations.** The agent must be unable to claim
  evidence it did not retrieve. Evidence should be carried as tool results
  rather than as prose the model wrote.

---

## Phases 4–9 — planned

**Phase 4, diagnosis.** Evidence-backed root-cause hypotheses. Extend the
`reasons`/`evidence`/`caveats` structure above rather than parallel it. A
hypothesis with no retrieved evidence behind it is a bug, not a low-confidence
answer.

**Phase 5, safe remediation.** A very small number of write actions —
`restart_container` and little else — behind the `LOW_RISK` tier. **This phase
is blocked on an architectural decision, described below.**

**Phase 6, verification.** A command that succeeded is not an incident that
resolved. Verification means re-measuring: health check passing, error rate
down, latency back to baseline, pressure down. Needs a bounded retry policy so
a failed remediation escalates instead of looping.

**Phase 7, human approval.** Approval gates for `HIGH_RISK`. The tier field
already exists; this phase adds the gate, the queue, and the audit trail.

**Phase 8, autonomous mode.** `OBSERVE` / `ASSIST` / `AUTONOMOUS` as
configuration. Only a predefined list of low-risk incident types may resolve
unattended.

**Phase 9, chaos environment.** A Docker stack with injectable failures — CPU
exhaustion, memory leak, container crash, disk exhaustion, connection
exhaustion, latency — to evaluate detection through verification end to end.
Never against real infrastructure.

---

## The open architectural decision

**There is currently no channel through which anything can act on an
instance,** and Phase 5 cannot start until that is chosen.

The agent pushes telemetry over one-way HTTP and has no inbound listener. That
is deliberate: the previous agent shipped a `POST /run-stress` endpoint on
`0.0.0.0:5001` that ran `stress` on any unauthenticated request — a remote
denial-of-service primitive on every instance in the fleet — and removing it
was one of the reasons for the rewrite. See `MIGRATION.md`.

Three options, with a recommendation:

1. **An inbound endpoint on each agent.** Rejected. This is the removed
   primitive with authentication bolted on.
2. **A poll-based work queue.** The agent polls for *approved* actions,
   executes from a fixed allowlist, and reports the result. No listening port,
   works behind NAT and security groups, and every action is a durable audited
   row before it executes rather than after. **Recommended.**
3. **Out-of-band execution** — SSM Run Command, or SSH from the control plane.
   The right answer for AWS-level actions such as instance or deployment
   changes; the wrong answer for restarting a container.

The choice shapes the audit and approval schema, so it should be made before
either is written.

---

## Known gaps

Things that are true right now and should not be discovered later by surprise.

- **The dashboard does not exist.** It was removed in the pivot with a stated
  rationale, and §9 of the SRE plan — incident timelines, AI activity, live
  incident detail — is a rebuild rather than an extension. The API is shaped
  to make that straightforward; the work is real regardless.
- **Unknown tool arguments are ignored rather than rejected.** `{"nonsense":
  1}` returns a normal result. Harmless for read-only tools, not acceptable
  once a write tool exists. Strict schemas are a Phase 5 prerequisite.
- **The fleet server has no authentication.** Fine for a private interface
  today. Not fine once anything downstream of it can act on a machine.
- **`get_node_health` has no notion of *when*.** Percentiles describe a whole
  window; they cannot say a problem started twelve minutes ago. See Phase 3.
- **CI does not run on this branch.** The workflow triggers on `main` and
  `pivot/**`; this work is on `syshealth-sre`. Pull requests still trigger it.
- **Nothing here has been evaluated against a real incident.** Every claim in
  this file is backed by recorded runs and unit tests, which is the right
  starting point and is not the same as evidence from production.
