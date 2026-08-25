# Migrating from the old SysHealth

This describes what happened to every file in the previous version, why, and
what you need to change if anything was already deployed.

Land this on a branch, not on `main`:

```bash
git checkout -b pivot/saturation
# copy this tree in, then:
git add -A && git commit -m "pivot: saturation-based right-sizing"
```

Then diff it against what you had and keep whatever you disagree with. Nothing
here is irreversible — `main` is untouched.

---

## What happened to each file

| Old | New | Note |
| --- | --- | --- |
| `collector.py` | `src/syshealth/procfs.py` | Rewritten. Reads all three PSI resources, both `some` and `full`, plus `meminfo` and `/proc/stat`. Every reader takes a `root`. |
| `analyzer.py` | `src/syshealth/analysis.py` | Rewritten. Baseline ratios replaced with absolute stall percentages. |
| `reporter.py` | `src/syshealth/render.py` | Rewritten. No longer writes files as a side effect of formatting. |
| `syshealth.py` | `src/syshealth/{cli,agent}.py` | Split. The agent no longer runs a control server. |
| `instance.py` | `src/syshealth/config.py` | IMDS auto-detection dropped; pass `--instance-type` or set it in config. |
| `server.py` | `src/syshealth/server.py` + `store.py` | State moved from an in-memory dict to SQLite. |
| `templates/`, `static/` | *(removed)* | See "The dashboard" below. |
| `deploy.sh` | *(removed)* | See "Deployment" below. |
| — | `src/syshealth/models.py` | New. `Interval` and `diff()`, the core measurement. |
| — | `src/syshealth/sampler.py` | New. Sampling loop with monotonic scheduling. |
| — | `src/syshealth/catalog.py` | New. Instance types and reference prices. |
| — | `src/syshealth/rightsize.py` | New. The verdict engine. |
| — | `tests/`, `tools/make_fixtures.py` | New. 87 tests, reproducible fixtures. |

---

## The five changes that need a decision from you

### 1. `avg10` → the `total=` counter

Old code read `avg10` from `/proc/pressure/memory`. That is a decaying average:
you cannot aggregate it, average it across a run, or compare two windows
meaningfully.

PSI also exposes `total=` in microseconds — a monotonic counter of time spent
stalled. Sampling it twice gives the exact stall time in between:

```
stall% = (total_us[t2] - total_us[t1]) / 1e6 / (t2 - t1) * 100
```

This is why the tool can now say "blocked 16.86% of wall-clock time" instead of
"PSI was around 17". `avg10` is still recorded for display, but no verdict
depends on it.

**If you have old `status.json` or agent logs, they are not comparable to new
output.** The numbers measure different things.

### 2. Baseline calibration is gone

Old flow: run `syshealth.py calibrate`, measure 60s idle, save `baseline.json`,
then classify by ratio against it.

Three problems. A ratio against an idle baseline is not comparable between
machines, so "5× baseline" meant something different on every box. An idle
machine has a baseline of zero, so the code fell back to a hardcoded `0.01`
and every subsequent ratio was measuring against a fudge constant. And it made
first-run setup a 60-second blocking step for no gain.

Replaced with absolute thresholds — percentage of wall-clock time stalled,
which means the same thing on a Raspberry Pi and a 24-core server. All of them
live in `Thresholds` in `analysis.py`.

**Delete any `baseline.json` files on your instances.** Nothing reads them.

### 3. The stress endpoint is removed

The old agent listened on `0.0.0.0:5001` and ran this on any unauthenticated
POST:

```python
subprocess.Popen(["stress", "--vm", "2", "--vm-bytes", "800M", ...])
```

That is a remote denial-of-service primitive on every instance in the fleet,
reachable by anyone who can route to the port, and it existed to make a
dashboard button work.

Load generation belongs in your hands, not in a daemon:

```bash
syshealth profile --instance-type t3.micro -- stress --vm 2 --vm-bytes 800M --timeout 120s
```

Same experiment, same numbers, and now it produces a verdict rather than just
moving a line on a chart. Nothing listens on a port to do it.

**Action: close port 5001 in your security group.** If old agents are still
running anywhere, kill them — they still have the endpoint.

### 4. State is now SQLite

`server.py` kept everything in a module-level `defaultdict`. A restart silently
discarded every measurement, including the ones you were about to make a sizing
decision from.

Now `store.py`, SQLite, WAL mode, retention enforced on write. Set the path with
`--db` or `db_path` in config.

There is no migration path for old data because there was no old data — it only
ever existed in memory.

### 5. No hardcoded addresses

Removed: `SERVER_URL = "http://13.61.11.18:5000/metrics"` in `syshealth.py`,
and `SERVER` + `AGENTS` in `deploy.sh`. A fresh clone was pushing measurements
at an IP that no longer belongs to anyone.

Everything now comes from config, environment, or flags, and `config.py` is the
only module that reads `os.environ`.

```bash
syshealth agent --server http://10.0.0.5:5000 --instance-type t3.small
# or
export SYSHEALTH_SERVER_URL=http://10.0.0.5:5000
```

Also fixed: `.gitignore` had a blanket `*.json`, which was silently excluding
any instance catalog or fixture you might commit. It is now specific.

---

## Command mapping

| Old | New |
| --- | --- |
| `python3 syshealth.py calibrate` | *(gone — no baseline)* |
| `python3 syshealth.py` | `syshealth agent --server URL` |
| `python3 server.py` | `syshealth serve --port 5000 --db fleet.db` |
| `curl -X POST /run-stress` | `syshealth profile -- stress ...` on the box |
| `GET /instances` | `GET /nodes` |
| `GET /instances/<h>/history` | `GET /nodes/<node>/samples` |
| `GET /series` | `GET /nodes/<node>/samples?limit=N` |
| — | `GET /nodes/<node>/verdict`, `GET /fleet`, `GET /healthz` |

New local commands with no old equivalent: `syshealth doctor`, `watch`,
`profile`, `report`.

---

## The dashboard

I removed `templates/index.html` and the 2,274 lines of CSS and JS in
`static/`, and did not replace them. That is the single biggest deletion here,
so it deserves a reason.

The dashboard was the project's centre of gravity, and it should not have been.
It showed PSI over time, which tells a viewer that a number moved but not what
to do. The verdict — a size, a cost, and the evidence — is the output that has
value, and it fits in a terminal.

The API is deliberately shaped so a UI is easy to add back: `GET /fleet`
returns every node's verdict and the fleet-wide monthly cost delta in one
response, which is roughly what a dashboard would want to render anyway.

**If your mentor expects a web UI, rebuild it against `/fleet` as a thin
client.** I'd suggest one screen: a table of nodes with current size,
recommended size, and dollar delta, sorted by how much money is on the table,
with the per-node time series behind a click. That is a day of work on top of
this API and it will demo far better than the old one, because the top of the
screen will have a number in dollars on it.

Your old `dashboard.js` is still in git history if you want to pull pieces of
the Chart.js wiring back out.

---

## Deployment

`deploy.sh` is gone. It hardcoded five IPs and a `.pem` path, `scp`'d loose
`.py` files into `~`, and restarted agents with `pkill -f`.

Now that the package is installable, use the packaging:

```bash
# on each instance
git clone <repo> && cd syshealth && pip install .
```

For a fleet, a systemd unit is more honest than `nohup`, because it restarts
on failure and survives a reboot:

```ini
# /etc/systemd/system/syshealth-agent.service
[Unit]
Description=SysHealth agent
After=network-online.target

[Service]
ExecStart=/usr/local/bin/syshealth agent
Environment=SYSHEALTH_SERVER_URL=http://10.0.0.5:5000
Environment=SYSHEALTH_INSTANCE_TYPE=t3.small
Restart=always
RestartSec=5
User=syshealth

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now syshealth-agent
```

I did not write an Ansible role or a Terraform module, because I don't know
what you already use. If you want reproducible EC2 provisioning for the
experiment, that is the obvious next piece.

---

## Verifying the pivot preserved your finding

Your README reported that under `stress --vm 2 --vm-bytes 800M`, t3.micro and
t3.small went CRITICAL while t3.medium and t3.large stayed healthy.

The `thrashing` fixture is modelled on that t3.micro result. The verdict engine
reaches the same conclusion independently:

```bash
syshealth report tests/fixtures/runs/thrashing.jsonl --instance-type t3.micro
# -> UNDERSIZED, recommends t3.medium
```

That is asserted as a test (`test_thrashing_run_is_undersized`), so the
finding is now regression-protected rather than a paragraph someone could
quietly contradict later.

---

## Suggested next steps

1. **Re-run your four-instance experiment with the new tool** and `--save`. The
   resulting JSONL files are the real artefact of the project — commit them
   under `experiments/`. Right now your evidence lives in a markdown table that
   nobody can re-analyse.
2. **Build the divergence experiment.** The `cache-heavy` scenario is currently
   synthetic. Reproducing it for real is straightforward: fill the page cache
   with `dd` on a real instance, show utilisation at 90%+ and PSI at zero, then
   show a thrashing workload where utilisation looks calmer and PSI spikes.
   Two real captured runs, side by side, *is* your major-project contribution.
   It is a falsifiable empirical claim, which is what a viva wants.
3. **Then** rebuild a UI, if you want one.

The order matters. The evidence is the project. The interface is packaging.
