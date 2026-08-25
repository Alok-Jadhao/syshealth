# Demo script

A run-through for presenting SysHealth as an autonomous SRE system, all
local, all stdio/localhost — nothing exposed to a network, nothing you have
to trust a screenshot for. Every command below has been run and its output
captured for real; where a number appears, it is a real number, not a mock.

**Read this once before presenting.** Then keep it open in a second window
while you drive the terminal.

---

## The 60-second pitch (say this first)

> "Most monitoring tools tell you a number moved. This one tells you *why it
> matters*, investigates itself, proposes a fix with the evidence attached,
> and — only if the fix actually worked when we measure again — closes the
> incident. It's not a dashboard with a chatbot bolted on. It's a policy
> engine that decides what an AI is allowed to do, a fixed catalogue of
> actions with zero shell access, and a full audit trail for every decision."

Then go straight into Track A.

---

## Before you present (do this once, ~2 minutes, not in front of anyone)

```bash
cd ~/Desktop/syshealth
source .venv/bin/activate
python tools/demo_setup.py
```

This builds `demo/fleet.db` from the project's own recorded, fixed-seed
scenarios — nothing invented for the demo:

```
  web-01     <- thrashing       (90 samples, t3.micro)
  cache-01   <- cache-heavy     (200 samples, t3.large)
  batch-01   <- idle-oversized  (400 samples, t3.2xlarge)

690 samples across 3 nodes -> demo/fleet.db
```

**If you're doing Track C (chaos)**, also warm the Docker build now so there's
no build wait live:

```bash
docker compose -f chaos/docker-compose.yml build
```

**If you're doing Track D (AI agent)**, register the MCP server now (see
Track D setup) so it's just "open a new chat" live.

If anything ever gets into a weird state mid-demo: `Ctrl-C`, then
`rm -rf demo && python tools/demo_setup.py` resets everything in under a
second — telemetry is fixture data, so this is fully repeatable.

---

## Track A — The core loop (safe backbone, ~5 min, no Docker needed)

This is the track to always run. It needs nothing but the venv and proves
detection, evidence-based diagnosis, and the "don't cry wolf" discipline.

### 1. Run the incident loop once

**Type:**
```bash
syshealth sre --db demo/fleet.db --incidents-db demo/incidents.db \
  --mode ASSIST --managed web-01=service:app --once
```

**What happens:** it sweeps all three nodes, opens an incident for anything
saturated, investigates each one with the read-only measurement tools, and
prints what it concluded as JSON. You'll see **two incidents with opposite
outcomes** — say this out loud, it's the whole point:

- `INC-1001` on `web-01` — a **real** problem:
  ```
  "cause": "memory exhaustion: the working set does not fit in RAM",
  "confidence": "HIGH",
  "status": "AWAITING_APPROVAL",
  "recommended": "restart_service(service='app')"
  ```
- `INC-1002` on `cache-01` — a **false alarm**, closed with no action taken:
  ```
  "cause": "no fault: high memory utilisation with no saturation",
  "status": "RESOLVED",
  "recommended": null
  ```

**Say this:** *"cache-01 is sitting at 94.5% memory 'used' — the number every
normal dashboard would page someone about. It measured that a machine
stalling 0.09% of the time waiting on memory is not broken, that's just
Linux using spare RAM for page cache, and it closed the incident **without
touching the machine**. That's the saturation-vs-utilisation distinction this
whole project is built on, now driving an actual decision."*

### 2. Show the full story for the real incident

**Type:**
```bash
syshealth incidents --incidents-db demo/incidents.db
```
**Output:**
```
ID         STATUS             SEV       NODE           TITLE
INC-1002   RESOLVED           WARNING   cache-01       cache-01: io degraded — stalled 2.0% at p95
INC-1001   AWAITING_APPROVAL  CRITICAL  web-01         web-01: memory saturated — tasks stalled 16.9% of wall-clock
```

**Type:**
```bash
syshealth incidents INC-1001 --incidents-db demo/incidents.db
```

**What to point at in the output** (scroll to each section as you talk):

- **DIAGNOSIS** — `confidence HIGH · by rules · citing #1, #2`. Say: *"Every
  claim it makes cites an evidence row it actually collected. If it cites an
  id that doesn't exist, the diagnosis is rejected outright and the incident
  goes to a human — that's not a prompt telling it to behave, it's a check
  that runs regardless of what a model says."*
- **observed** vs **inferred** — two separate lists. Say: *"Measured facts and
  inferences from them are never allowed to blur into one list."*
- **EVIDENCE** — `#1 get_node_health(node='web-01')`, `#2
  get_node_verdict(...)`. The actual tool calls, numbered, permanent.
- **ACTIONS** — `AWAITING_APPROVAL restart_service(service='app') [LOW_RISK]`,
  with the `why:` and `policy:` lines right there. Say: *"It didn't just pick
  an action, it picked one from a fixed catalogue — five actions total, no
  shell, no arbitrary commands, and the policy line tells you exactly which
  rule made it stop and ask."*
- **TIMELINE** — every step, timestamped, in order.

---

## Track B — Human approval, live (visual, ~3 min)

**Type:**
```bash
syshealth serve --host 127.0.0.1 --port 5050 \
  --db demo/fleet.db --incidents-db demo/incidents.db
```
Open **http://localhost:5050** in a browser.

**What you'll see:** fleet tiles at the top (1 healthy, 1 degraded, 1
saturated, matching what the CLI just showed), an **"Awaiting approval"**
card for `restart_service(service='app')` with the reason and the policy
ruling printed right on it, and the incident list below with the full
diagnosis, evidence table, and timeline — the same report, rendered.

Click **Approve**, type a name when prompted. The card disappears from
"Awaiting approval" and the action's status flips to queued for the node to
collect.

**Say this:** *"Every action tier maps to whether this box exists at all. A
LOW_RISK action can be listed here for approval or — in autonomous mode, only
if it's on an explicit allow-list — run without asking. A HIGH_RISK action
like terminating an instance requires a human in **every** mode, including
autonomous. That's not a default, it's checked in code and there's a test
asserting it can't regress."*

**If someone asks "then what?"**: the approved action sits `DISPATCHED`,
waiting for the real node's `syshealth executor` process to poll for it,
run it from its own allowlist (never a string it was handed), and report
back. A command succeeding moves the incident to `VERIFYING` — **never**
straight to resolved. Only a fresh measurement showing the stall actually
fell, the state actually improved, and the node is still reporting closes it.
If the fixture data doesn't change (as here, since it's a replay), the loop
correctly reports "not recovered" and retries rather than lying — that
retry/escalate path is bounded by an attempt limit so it can never spin
forever, tested in `test_remediation_stops_after_the_attempt_limit`.

```bash
# clean up before the next track
Ctrl-C
```

---

## Track C — Chaos: a real broken container (the wow factor, ~6–8 min)

Everything up to now replayed recorded data. This track injects a **real**
memory problem into a **real** Docker container and watches the system find
it on its own kernel, live.

### 1. Bring it up

**Type:**
```bash
./chaos/demo.sh up
```
**What happens:** builds and starts a small Flask app in a container capped
at 256MB, starts the fleet server, and starts an agent that measures the
container's own cgroup (not the host — `/proc/pressure` is host-wide, so this
needed its own reader; mention that if asked how).

### 2. Break it, on purpose

**Type:**
```bash
./chaos/demo.sh churn 150
```
This reads a 512MB file inside a 256MB limit, over and over. **Say while it
runs:** *"This isn't a fake metric — it's forcing the kernel to genuinely
evict and refetch pages it needs again. That's real memory pressure, not a
number I'm typing in."*

**Wait ~45 seconds** for pressure to build (do Track A/B talking points here
if you need to fill the time, or just narrate the mechanism above).

### 3. Point it at the container

**Type:**
```bash
./chaos/demo.sh sre ASSIST
```
**What you'll see** — a real diagnosis, computed from live kernel counters
this run:
```
"cause": "memory saturation: sustained stalling without OOM or swap-in",
"confidence": "MEDIUM",
```

**Type:**
```bash
./chaos/demo.sh report
```
**The line to read out loud**, from the reasoner's own inference:

> *"Without OOM kills or swap-in, this is more consistent with heavy
> page-cache churn than with a hard leak — a workload reading more than
> fits, rather than a process growing. **Restarting a container would not fix
> cache churn.** Confirm which process is responsible before acting."*

**Say this:** *"That's exactly correct for what I just did to it, and the
system reasoned its way there from raw pressure counters — it didn't restart
the container, because restarting wouldn't have fixed anything, and it said
so."*

### 4. The bugs this environment actually found

This is worth a slide of its own. Say:

> "This container is also how we caught two real bugs that 250 unit tests
> missed, because a static fixture can't produce the scenario. A container
> stalling 2.9% on memory and 52% on IO at the same time was being *titled*
> a memory incident, because the detector took the first resource over
> threshold instead of the worst one — so the incident title and the
> investigation contradicted each other. And saturation showing up as
> **full**-stall — windows where *nothing on the machine ran at all* — was
> falling through a check that only looked at the `some` share, so it was
> reported as 'no fault found.' Both are fixed and now have regression
> tests. Chaos testing wasn't a checkbox here, it changed the code."

### 5. Tear down

```bash
./chaos/demo.sh down
```

---

## Track D — Natural language over MCP (optional, ~4 min, needs setup ahead of time)

This is the piece that proves the tool layer speaks a standard protocol
rather than a bespoke API — any MCP client can drive it, including an AI
agent asking plain questions.

### Setup (do this before you're in front of anyone)
```bash
claude mcp add syshealth -- \
  "$(pwd)/.venv/bin/syshealth" mcp --db "$(pwd)/demo/fleet.db" --fleet-only
```
Then open a **fresh** Claude Code / Claude Desktop chat (the current one
won't see a server registered after it started).

### Live
Ask, in plain English:
- *"What's the health of this fleet?"*
- *"What's wrong with web-01, and how confident should I be?"*
- *"Is cache-01 actually oversized, or is that just cache?"*

**Say while it responds:** *"Watch — it's not answering from memory, it's
calling `get_node_health` and `get_node_verdict` live, right now, and every
tool it can see is hinted read-only at the protocol level. There is no tool
in this server that can change anything — that's not a prompt instruction,
it's what's registered."*

If you want to show the schema itself rather than trust the model's behavior,
`npx @modelcontextprotocol/inspector` against the same command gives a raw
GUI over tool discovery and invocation — useful for a skeptical judge who
wants to see the protocol, not the chat.

---

## If you only have 5 minutes

Track A only: run the `sre --once` command, then `syshealth incidents
INC-1001`. That alone shows evidence-based diagnosis, the false-alarm
discipline, and the audit trail — the three hardest things to fake.

## If you only have 90 seconds

Run the pitch, then just:
```bash
syshealth sre --db demo/fleet.db --incidents-db demo/incidents.db --mode ASSIST --managed web-01=service:app --once
```
and read the two `"cause"` lines out loud — one real, one correctly dismissed.

---

## Talking points cheat-sheet (for narration and Q&A)

Use these to answer "why is this hard" or "what's actually novel here":

| Point | Where it showed up | The specific, checkable claim |
| --- | --- | --- |
| Saturation ≠ utilisation | INC-1002 closing with no action | 94.5% "used", 0.09% stalled — measured, not asserted |
| Evidence citation is enforced | `citing #1, #2` in DIAGNOSIS | A citation to evidence that wasn't collected gets the diagnosis **rejected**, not flagged low-confidence — `test_a_diagnosis_citing_evidence_that_does_not_exist_is_rejected` |
| No shell, ever | ACTIONS section, `[LOW_RISK]` tag | 5 actions total, typed arguments, no `run_command`. An AST-parsing test fails the suite if `shell=True` or `os.system` ever appears in the executor |
| Confidence can't buy permission | policy always asks the same way regardless of confidence | `Policy.rule()` has no `confidence` parameter at all — asserted by a test on its exact signature |
| HIGH_RISK always needs a human | mentioned in Track B | True in every mode including AUTONOMOUS — regression-tested explicitly |
| Success ≠ resolved | Track B, `next: "VERIFYING"` | A command returning exit 0 moves to VERIFYING; only a second measurement showing real recovery closes the incident |
| The loop can't run forever | attempt-limit talking point | Bounded by an attempt counter every retry path passes through — no edge skips it |
| Real bugs from real chaos | Track C step 4 | Two correctness bugs, found only under genuine kernel pressure, now regression-tested |
| Standard protocol, not a bespoke API | Track D | MCP: any client can discover and call these tools, not just this one app |

## Numbers to have ready

- **251 tests**, passing with no PSI kernel, no MCP SDK, and no API key required
- **9 phases** built: MCP foundation → fleet integration → investigation →
  diagnosis → remediation → verification → approval → autonomy → chaos
  environment (`PHASES.md` has the full record, including the known gaps —
  worth mentioning you documented what's *not* done too, e.g. no auth on the
  HTTP API yet)
- **5 actions** in the entire remediation catalogue, **0** of which take a
  shell command
- **2 real bugs** found by the chaos environment that unit tests missed

## Known limits — say these before someone else finds them

Owning these earns more credibility than hiding them:

- The dashboard/API has no authentication — fine on localhost for a demo,
  explicitly called out in `PHASES.md` as the next piece of real work
  before this touches anything outside a private network
- The Claude-backed reasoner exists and is unit-tested, but has not been run
  against the live API in this repo — the deterministic rule-based reasoner
  is what's driving everything you just saw
- Detection covers saturation (memory/cpu/io pressure); it doesn't yet see
  application-level symptoms like latency or error rate — that needs a
  different telemetry source, not a bigger prompt
