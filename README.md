# SysHealth

**Your monitoring says the server is at 94% memory. It is fine. Your monitoring says the other one is at 60%. It is dying.**

SysHealth measures *saturation* — the share of wall-clock time your machine spent unable to make progress — instead of *utilisation*, the share of a resource that is occupied. It then tells you what size the machine should actually be.

```
$ syshealth profile --duration 10m --instance-type t3.micro -- ./run-benchmark.sh

  SATURATION  (share of wall-clock time stalled)
    memory  p50  14.35%  p95  16.86%  max  16.97%   █████████████··· SATURATED
    cpu     p50   0.73%  p95   1.31%  max   1.37%   █··············· DEGRADED

  UTILISATION  (what a normal dashboard shows)
    memory  used/free rule   peak  95.0%   <- what most dashboards show
            working set      peak  95.0%   of 0.95 GB

── verdict ─────────────────────────────────────
  UNDERSIZED   (high confidence)
  Undersized on memory. Stalled 16.86% of wall-clock time at p95
  (24.8s lost to waiting across the run).

    current      t3.micro         1 GB   2 vCPU   $   7.59/mo
    recommended  t3.medium        4 GB   2 vCPU   $  30.37/mo
    extra        $22.78/month  ($273.31/year per instance)
```

---

## The problem

Almost every autoscaling rule, capacity alert and right-sizing tool is built on utilisation: CPU percent, memory percent, disk percent. Utilisation is a bad proxy for whether anything is actually wrong.

Memory is the worst offender. Linux fills otherwise-idle RAM with page cache, so a perfectly healthy server routinely reports 90%+ "used". Teams see the number, get alarmed, and provision a bigger box. Meanwhile a machine reporting a comfortable 60% can be in continuous direct reclaim — evicting pages it is about to need again — and the utilisation graph shows nothing unusual at all.

The result is the two failure modes that cost real money: **paying for headroom nobody needed**, and **not noticing the box that is quietly thrashing**.

Since Linux 4.20 the kernel has exposed the number that actually answers the question. Pressure Stall Information (`/proc/pressure/*`) reports how long tasks spent blocked waiting for memory, IO or CPU. It is a direct measurement of lost progress rather than a proxy for it. It is also almost entirely unused outside a handful of large infrastructure teams.

## What SysHealth does

1. **Measures saturation properly.** It reads the PSI `total=` microsecond counter, not the smoothed `avg10` field, so a stall figure is an exact accounting of time lost between two samples rather than a decaying average that cannot be aggregated or compared.
2. **Shows saturation next to utilisation.** Every report contrasts the two, so the divergence is visible rather than theoretical.
3. **Turns that into a sizing decision.** A verdict with an instance type, a monthly cost delta, the reasoning, and the evidence behind it.
4. **Refuses to guess.** No PSI, too few samples, or too short a run, and it says so rather than inventing a confident answer.

## Install

```bash
git clone https://github.com/Alok-Jadhao/syshealth.git
cd syshealth
make install          # venv + dev/server extras
source .venv/bin/activate
syshealth doctor      # tells you whether this machine can be measured
```

The core has **no runtime dependencies**. `doctor`, `watch`, `profile` and `report` work on a freshly provisioned box with nothing but the system Python 3.10+. Flask is needed only for the optional fleet server.

```bash
pip install -e .              # core only
pip install -e '.[server]'    # adds the fleet server
```

### If you have no PSI kernel

macOS, most containers, and older kernels have no `/proc/pressure`. The tool still runs — every analysis path is exercisable against recorded runs:

```bash
make demo    # all three scenarios, no kernel support needed
```

## Usage

```bash
syshealth doctor                              # can this machine be measured?
syshealth watch                               # live saturation, one line per sample
syshealth profile --duration 15m              # measure a window
syshealth profile -- ./my-benchmark.sh        # measure a command until it exits
syshealth profile --duration 30m --save run.jsonl --instance-type t3.small
syshealth report run.jsonl --instance-type t3.small --json
```

Profiling a command is the most useful mode: it answers "is this box the right size for *this workload*" with evidence, which is the question you actually have.

Exit codes make it usable as a gate in CI or a deploy pipeline:

| Code | Meaning |
| ---- | ------- |
| 0 | fine, or oversized |
| 1 | error |
| 2 | **undersized** — the workload saturated the machine |
| 3 | PSI unavailable, nothing was measured |

```bash
syshealth profile --duration 5m -- ./load-test.sh || echo "needs a bigger box"
```

### Fleet mode

For monitoring many machines, agents push measurements to a central server, which runs the same verdict engine over each node's history.

```bash
# server
syshealth serve --host 0.0.0.0 --port 5000 --db fleet.db

# on each machine
syshealth agent --server http://10.0.0.5:5000 --instance-type t3.small
```

| Endpoint | Description |
| -------- | ----------- |
| `POST /metrics` | agent push |
| `GET /nodes` | every node with online status |
| `GET /nodes/<node>/samples` | recent measurements |
| `GET /nodes/<node>/verdict` | sizing verdict for one node |
| `GET /fleet` | every verdict plus the fleet-wide monthly cost delta |
| `GET /healthz` | liveness |

`GET /fleet` is the one that matters: it totals what the whole fleet is over- and under-provisioned by, in dollars per month.

> The server has **no authentication**. Bind it to a private interface, a security group, a VPN or a reverse proxy. It warns you if you bind `0.0.0.0`.

## Configuration

Nothing deployment-specific is hardcoded. Settings resolve in this order, later winning:

1. defaults
2. `syshealth.toml` (`--config`, `SYSHEALTH_CONFIG`, `./syshealth.toml`, `~/.config/syshealth/config.toml`)
3. `SYSHEALTH_*` environment variables
4. command line flags

```toml
[syshealth]
interval_s    = 2.0
instance_type = "t3.medium"
server_url    = "http://10.0.0.5:5000"
db_path       = "/var/lib/syshealth/fleet.db"
```

Prices in the built-in catalog are **reference values** for us-east-1 on-demand Linux and are not live. Override them:

```bash
syshealth report run.jsonl --catalog ./my-prices.json
```

## How the verdict is reached

Thresholds are percentages of wall-clock time stalled, so they mean the same thing on a Raspberry Pi and a 24-core server. All of them live in one place, `Thresholds` in `analysis.py`.

| State | Condition |
| ----- | --------- |
| HEALTHY | `some` stall below 1% |
| DEGRADED | `some` stall 1–10%, or `full` stall above 0.5% |
| SATURATED | `some` stall at or above 10%, or `full` stall at or above 2% |

`some` means at least one task was stalled. `full` means *nothing* ran — every runnable task was blocked. Some `some` pressure is normal on a busy server; sustained `full` pressure never is, which is why its thresholds are lower.

The run-level state uses the p95, not the max, so one transient spike does not condemn an otherwise healthy machine.

Sizing then follows from the state:

- **Saturated** → step up one size, or two if there were OOM kills, swap-in, or `full` stall above 5%. Reported as a floor, not a target: a machine that stalled may have a working set larger than anything observable on it.
- **Healthy** → find the smallest type that still covers the observed peak working set plus 30% headroom. The working set is derived from `MemAvailable`, which already excludes reclaimable page cache — this is what stops the recommendation being fooled by cache the way a `used`-based figure would be.
- **Not enough evidence** → no recommendation. Runs shorter than 5 minutes never produce downsizing advice, because a quiet 60-second window is not proof that a box can be shrunk.

## Development

```bash
make test     # 87 tests, no kernel support required
make cov      # with coverage
make lint     # ruff
make fixtures # regenerate recorded runs
```

CI runs on Ubuntu **and macOS**, on purpose: the suite must pass without a PSI kernel. That is what proves a contributor can develop on any laptop, and it is enforced rather than hoped for.

Recorded scenarios in `tests/fixtures/runs/` are generated by `tools/make_fixtures.py` from a fixed seed, so they are reproducible and reviewable in a diff rather than being opaque blobs:

| Scenario | What it demonstrates |
| -------- | -------------------- |
| `thrashing` | genuine memory saturation — must say UNDERSIZED |
| `cache-heavy` | 94% "used", zero stalling — the false alarm; must **not** say grow |
| `idle-oversized` | a large box doing little — the money case |

## Layout

```
src/syshealth/
  procfs.py     kernel readers; every one takes a root, which is why this is testable
  models.py     Snapshot and Interval; diff() is the core measurement
  sampler.py    turns a reader into a stream of Intervals
  analysis.py   thresholds, classification, run summaries
  catalog.py    instance types and reference prices
  rightsize.py  the verdict engine
  render.py     terminal output
  config.py     the only module that reads the environment
  store.py      sqlite persistence for the fleet server
  agent.py      push agent, stdlib only
  server.py     fleet API (needs Flask)
  cli.py        argparse entry point
```

## Limitations

- **Linux only, kernel 4.20+ with `CONFIG_PSI=y`.** Some distributions also need `psi=1` on the kernel command line. Containers usually do not expose the host's PSI; run the agent on the host, or bind-mount `/proc` and pass `--proc-root /host/proc`.
- **A verdict only covers what ran during the measurement.** Profile a representative peak, not a quiet afternoon, before resizing anything in production.
- **Prices are reference values.** They ignore reserved instances, savings plans, spot, and every region but us-east-1.
- **Sizing is single-machine.** It answers "how big should this box be", not "should this be three boxes".

## Licence

MIT.
