# Chaos environment

A deliberately imperfect service in a memory-limited container, so the
incident loop can be evaluated against something that is really broken rather
than against a fixture that describes being broken.

```bash
./chaos/demo.sh up            # faulty app + fleet server + agent
./chaos/demo.sh churn         # sustained memory pressure
sleep 45
./chaos/demo.sh sre ASSIST    # detect -> investigate -> diagnose -> propose
./chaos/demo.sh report        # the whole audit trail
./chaos/demo.sh down          # remove everything
```

**Never point this at real infrastructure.** The app exposes unauthenticated
endpoints whose entire purpose is to break it.

## Why the container is measured, not the host

`/proc/pressure/*` is host-wide. An agent inside a container either finds
nothing there or finds the host's numbers and reports them as the container's,
which would make "which container saturated this box?" unanswerable — and that
is the only question a chaos environment exists to ask.

cgroup v2 exposes the same PSI counters per control group, so
`syshealth agent --cgroup /sys/fs/cgroup/.../docker-<id>.scope` attributes
saturation to the thing that caused it. `--container <id>` resolves the path
for you. This needs cgroup v2 and `CONFIG_PSI=y`; on cgroup v1 hosts
per-container PSI does not exist at all and the agent says so rather than
quietly measuring the host.

The `mem_limit: 256m` in the compose file is what makes the experiment work.
Without a limit there is nothing for the app to saturate short of the whole
machine, and any incident would be about the host instead of the service.

## The scenarios

| Command | What it does | What the kernel sees |
| ------- | ------------ | -------------------- |
| `churn` | reads a 512MB file repeatedly inside a 256MB limit | continuous reclaim of file pages: sustained memory *and* io stalling, container survives |
| `leak` | allocates and touches memory past the limit | an OOM kill — anonymous memory with no swap cannot be reclaimed, so it dies rather than stalls |
| `burn` | busy loops on every core | cpu pressure |
| `crash` | exits the process abruptly | the container stops |
| `reset` | frees everything, clears injected faults | what a successful remediation looks like |

`churn` and `leak` are both worth running, because they fail differently.
Growing anonymous memory in a cgroup with no swap ends in an OOM kill before
PSI has much to say; file-backed pages are reclaimable, so reading more than
fits produces real, sustained stalling on a container that stays alive. The
second is the more common production failure and the more interesting test.

## What it has already caught

Two bugs, neither visible to the unit tests, both now regression-tested in
`tests/test_sre_loop.py`:

**Incidents were titled by the wrong resource.** A container stalling 2.9% on
memory and 52% on io was reported as a memory incident, because the detector
returned the first resource crossing a threshold rather than the worst. The
investigation then contradicted the incident it was opened for.

**Saturation via full-stall was missed.** The reasoner tested only the `some`
share, so a container whose full-stall reached 3% — windows in which *nothing
ran* — fell through to "no fault found".

That is the argument for the environment existing. Both were correctness bugs
in the reasoning path, and both needed a real kernel under real pressure to
surface.

## What it does not cover

- **One node.** Blast-radius and concurrency guards are unit-tested, not
  exercised here.
- **No remediation loop by default.** `demo.sh sre` runs in ASSIST, so it
  proposes and stops. Running `AUTONOMOUS` with `--autonomous-actions
  restart_container` will actually restart the container — which is the point,
  but do it knowingly.
- **Not a load test.** The app is a toy. It demonstrates failure modes; it
  does not represent production traffic.
