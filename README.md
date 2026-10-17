# SysHealth — Find the right instance size for your app

SysHealth tells you **which instance size to buy for your app, and shows the
evidence**. It runs the memory your app needs on several sizes at once, measures
how long programs wait for memory on each (Linux PSI, Pressure Stall
Information), and names the cheapest size that holds your app's peak with room
to spare, in dollars a month. A Flask server collects the agents' samples and
serves a live dashboard; the answer is also one API call away.

### Why not just Prometheus?

Prometheus with node_exporter already collects memory PSI, and Grafana can graph
it. If all you need is to *watch* pressure, use them. SysHealth does what a
monitor cannot, because a monitor only observes:

| | Prometheus + Grafana | SysHealth |
|---|---|---|
| Memory pressure (PSI) over time | Yes | Yes |
| Thresholds for "degraded" and "critical" | You write the rules | Built in, from published production systems ([docs](docs/thresholds.md)) |
| Applies a load, the same on every size at the same moment | No | Yes: peak, ramp, up-and-down, bursts |
| Knows what load caused a reading | No | Yes, every sample carries the test step |
| How much memory each size can hold before your app slows down | No | Yes, from the pressure at each load level |
| Which size to buy, and what it costs | No | Yes: the cheapest size that fits, priced, with what the others would cost |
| Why a machine is under pressure | Raw counters; you work it out | The likely cause in words (swapping, programs filling RAM, files re-read from disk), who holds the memory, and any process that only grows |
| Compares itself with a usage alarm | No | Yes: what "alarm at 80% in use" would have bought instead, and why it would be wrong when it is |

They also work together: SysHealth serves everything it knows in Prometheus's
format at `GET /metrics`, the states, the cause data and the last test's verdict
included ([below](#with-prometheus)).
| Says it in plain words | No | "Choose t3.medium · $30.37 a month. It holds about 3.2 GB before your app slows down…" |

---

## Architecture

```
EC2 Agent (t3.micro)  ──┐
EC2 Agent (t3.small)  ──┤──► Central Server (Flask :5000)  ◄──  Browser Dashboard
EC2 Agent (t3.medium) ──┤         ▲
EC2 Agent (t3.large)  ──┘         │ Stress trigger (:5001)
                                   └──────────────────────
```

Each agent:
- Reads `/proc/pressure/memory` (PSI `some` and `full`, avg10) and `/proc/vmstat` every 5 seconds
- Classifies health as **HEALTHY / DEGRADED / CRITICAL** against fixed, research-backed thresholds ([below](#health-classification))
- POSTs a JSON payload to the central server
- Runs a control server on port 5001 that runs the load tests the dashboard plans, and stops them

---

## Dashboard

Open `http://YOUR_SERVER_IP:5000` in your browser.

- **One number, explained.** The headline is the share of the last minute that
  programs spent waiting for memory, 0–100%. It sits on a Healthy / Degraded /
  Critical band scale, with a plain-English line under it ("out of every 10
  seconds, programs spent about 1.4 waiting for memory") and what to do about it.
  **What the number means** expands into the three bands.
- Live cards per instance — green dot (online) / grey dot (offline), RAM, and the
  same band scale, so sizes compare at a glance
- Memory-pressure graph over time, auto-updating, with the one-minute average
  the state is judged on, and the Degraded (10%) and Critical (40%) lines drawn
  in on every view — they are the same for every size
- **Instance** toggle — one machine on its own, or every size overlaid to
  compare. Colour encodes instance size on a single light-to-dark ramp, so a
  line's shade tells you how big the box is
- **Range** toggle — 5m / 15m / 1h / All — and a table view of the same samples
- **⚡ Load test** — a form for the load your app actually has, run on every
  online instance at the same moment ([below](#load-tests)). The test's load is
  drawn as a track above the graph, so pressure lines up with what caused it
- **Where the memory is** — for one machine: programs, file cache, the
  kernel and free, how much is "in use" the way most dashboards count it, the
  processes holding the most, the **likely cause** of any pressure, and a
  warning for a process that has only grown for minutes, with when the
  machine would run out at that rate ([below](#why-the-likely-cause))
- **Which size to choose** — after a test, the cheapest size that holds your
  app's peak with the room to spare you ask for, priced per month, with a chart
  of pressure against load that shows where each size gives out
  ([below](#right-sizing-which-size-to-buy))

---

## Experimental Results

Same stress test (`stress --vm 2 --vm-bytes 800M --vm-keep`) run on all four instance types simultaneously:

| Instance Type | RAM | Peak PSI | State (old 2×/5× rule) |
|---|---|---|---|
| t3.micro | 1 GB | 17.10 | **CRITICAL** |
| t3.small | 2 GB | 11.05 | **CRITICAL** |
| t3.medium | 4 GB | 0.00 | **HEALTHY** |
| t3.large | 8 GB | 0.00 | **HEALTHY** |

**Finding:** t3.medium (4 GB RAM) is the minimum instance type that stays HEALTHY under a 1.6 GB memory workload.

> These states were assigned under the old baseline-ratio rule, which the
> thresholds below replace, and `full` pressure was not recorded then. By peak
> `some` alone, micro and small fall in the new Degraded band (10–40%). The
> micro also had its job killed by the OOM killer, which the load-test results
> now report directly. Re-run with a load test before quoting these.

---

## Preview it locally

The dashboard only says anything once several differently-sized machines are
pushing, so there is a demo mode that simulates a four-size fleet — no AWS, no
agents, nothing to configure:

```bash
sudo apt install python3-flask python3-requests    # first time only
npm run dev                                        # or: python3 dev.py
```

On Debian and Ubuntu, `pip install flask` fails with
`externally-managed-environment` — pip is not allowed to write to the system
Python. Install the packaged versions as above, or keep them out of it entirely:

```bash
python3 -m venv .venv
.venv/bin/pip install flask requests
source .venv/bin/activate      # then: npm run dev
```

Then open <http://127.0.0.1:5000>. You get `t3.micro` through `t3.large` under a
repeating load cycle, the **Instance** toggle to compare them, and **Load test**,
which runs a simulated peak or variable test of whatever size you enter.

The pressure values are synthetic and every page says so in a banner. Only the
sensor is faked: each simulated machine turns memory demand into pressure from
its own RAM, then goes through the real `Analyzer`, and tests are planned by the
real `loadtest.py`. So the states, the reasons, the threshold lines and the
load-test results come out of the same code that runs in production.

`npm` is used only as a familiar entry point — the project is Python, and
`package.json` has no dependencies to install.

---

## Provision the real thing

There is Terraform for the dashboard plus one instance of each size. Two
variables to set, one command to run:

```bash
cd terraform

# -4 matters: many ISPs answer with IPv6 by default, and the rules are IPv4.
cat > terraform.tfvars <<EOF
key_name = "your-keypair"
ssh_cidr = "$(curl -4 -s ifconfig.me)/32"
EOF

terraform init && terraform apply
```

Everything installs and starts itself; open the `dashboard_url` output. Give it
two to three minutes, and run `./check.sh` to see which sizes are reporting and
which are not. See [`terraform/README.md`](terraform/README.md) for costs, design
notes and `terraform destroy`.

---

## Quick Start (manual)

### 1. Launch EC2 instances (AWS CLI)

```bash
# Key pairs and security groups are both per region, so set this once.
REGION=ap-south-1

# Create key pair
aws ec2 create-key-pair --region $REGION --key-name syshealth-key \
  --query "KeyMaterial" --output text > syshealth-key.pem
chmod 400 syshealth-key.pem

# Create security group (ports 22, 5000, 5001)
SG_ID=$(aws ec2 create-security-group --region $REGION \
  --group-name syshealth-sg --description "SysHealth" \
  --query "GroupId" --output text)

aws ec2 authorize-security-group-ingress --region $REGION \
  --group-id $SG_ID --ip-permissions \
  "IpProtocol=tcp,FromPort=22,ToPort=22,IpRanges=[{CidrIp=0.0.0.0/0}]" \
  "IpProtocol=tcp,FromPort=5000,ToPort=5000,IpRanges=[{CidrIp=0.0.0.0/0}]" \
  "IpProtocol=tcp,FromPort=5001,ToPort=5001,IpRanges=[{CidrIp=0.0.0.0/0}]"
```

### 2. Set up the central server

```bash
ssh -i syshealth-key.pem ubuntu@YOUR_SERVER_IP

sudo apt update && sudo apt install -y python3-pip
pip3 install flask requests
git clone https://github.com/Alok-Jadhao/syshealth.git && cd syshealth
nohup python3 server.py > server.log 2>&1 &
```

### 3. Set up agents on each EC2 instance

```bash
ssh -i syshealth-key.pem ubuntu@YOUR_AGENT_IP

sudo apt update && sudo apt install -y python3-pip stress
git clone https://github.com/Alok-Jadhao/syshealth.git && cd syshealth

# Point agent at your server
sed -i 's|http://127.0.0.1:5000/metrics|http://YOUR_SERVER_IP:5000/metrics|' syshealth.py

# Calibrate baseline (60s idle measurement)
python3 syshealth.py calibrate

# Start agent
nohup python3 syshealth.py > agent.log 2>&1 &
```

The instance appears on the dashboard within 5 seconds.

---

## Deploy Script

After making local changes, push to all instances with one command:

```bash
# Deploy agent changes to all EC2 instances
./deploy.sh agents

# Deploy server + dashboard changes
./deploy.sh server

# Deploy everything
./deploy.sh
```

Edit the `AGENTS` and `SERVER` variables at the top of `deploy.sh` to match your IPs.

---

## Health Classification

The number is Linux PSI: the share of time programs were stalled waiting for
memory. 20% means that out of every 10 seconds, 2 were lost waiting, so the work
could have run about 20% faster with enough memory. It measures harm, not usage:
a box can be 90% full and fine.

| State | Condition (one-minute average) | In plain words | Persistence |
|---|---|---|---|
| HEALTHY | `some` < 10% | Under 1 s in every 10 lost to waiting | Immediate |
| DEGRADED | `some` 10–40% | Noticeably slower than with enough memory | 3 consecutive samples (15s) |
| CRITICAL | `some` ≥ 40%, or `full` ≥ 10% | Thrashing; timeouts and OOM kills are next | 3 consecutive samples (15s) |

`some` is the share of time *any* task waited; `full` is the share of time
*every* task did, so nothing got done at all.

The thresholds are absolute: PSI is already normalised to wall-clock time, so
they mean the same on every size. They are taken from systems that act on PSI in
production — Android's lmkd, systemd-oomd, Meta's oomd and the TMO paper (ASPLOS
'22). **[`docs/thresholds.md`](docs/thresholds.md)** has the sources, the
reasoning for each line, and why the old 2×/5×-of-baseline rule was dropped.
The values are constants at the top of `analyzer.py`, and every sample carries
the ones it was judged by, so the dashboard draws whatever was applied.

**Baseline calibration** still runs (`python3 syshealth.py calibrate`) and is
reported as each machine's idle reference, but states no longer depend on it.

---

## REST API

| Method | Endpoint | Description |
|---|---|---|
| GET | `/` | Live dashboard |
| POST | `/metrics` | Agent metric push |
| GET | `/metrics` | Everything above for Prometheus to scrape ([below](#with-prometheus)) |
| GET | `/instances` | All instances with online status and latest metrics |
| GET | `/instances/<hostname>/history` | Last 60 samples for one instance |
| GET | `/series` | Every instance's recent history — what the graph reads |
| POST | `/load-plan` | Preview a load test: the plan and the commands, without running it |
| POST | `/run-stress` | Run a load test on all online agents (no body: the old default test) |
| POST | `/stop-stress` | Stop the running load test on all online agents |
| GET | `/report` | Which size to buy, from the last test: `?need_mb=` your app's peak, `?margin=` % spare, `?test_id=` an earlier test |

```bash
# List all instances
curl http://YOUR_SERVER_IP:5000/instances

# The old fixed test: 1.6 GB (2 workers x 800 MB) for 60 s
curl -X POST http://YOUR_SERVER_IP:5000/run-stress

# A variable test: ramp 200 MB -> 1.6 GB in 5 steps of a minute
curl -X POST http://YOUR_SERVER_IP:5000/run-stress -H 'Content-Type: application/json' \
  -d '{"mode":"variable","pattern":"ramp","low_mb":200,"high_mb":1600,"steps":5,"step_sec":60,"workers":2}'

curl -X POST http://YOUR_SERVER_IP:5000/stop-stress

# Which size to buy for an app that peaks at 1.2 GB, keeping 30% spare
curl 'http://YOUR_SERVER_IP:5000/report?need_mb=1200&margin=30'
```

---

## Load tests

The load is no longer fixed. Open **⚡ Load test** on the dashboard and describe
your app's memory use. Every online instance runs the same plan at the same
moment.

| Test | Fields | Answers |
|---|---|---|
| **Peak load** | Memory to hold (MB), how long | Can each size take my app at its busiest? |
| **Variable load: ramp up** | Your app's peak, from / to (MB), steps, seconds per step | How much can each size hold before it slows down? |
| **Variable load: up and down** | Same | A day of traffic: does each size recover when load drops? |
| **Variable load: bursts** | Base / spike (MB), how many, seconds each | How does each size take short spikes? |

Before anything runs, the panel previews the plan as a chart, with each
instance's RAM drawn on it. It shows the peak as a share of that RAM ("168% of
t3.micro's memory") and the exact `stress` command each step becomes. While it
runs, a chip in the top bar shows the step and time left, and **Stop test** ends
it everywhere. Afterwards the panel says which size to choose
([below](#right-sizing-which-size-to-buy)) and reports, per instance, the
outcome, how much it holds, the room it leaves, its price, the worst minute and
the sharpest spike.

Each step is one run of `stress --vm <workers> --vm-bytes <MB per worker>M
--vm-keep --timeout <s>s`. A step always lasts its planned time, even if the OOM
killer ends `stress` early, so every size stays in step. The early exit is
counted and reported as "ran out of memory".

The plan is built and bounded in one place, `loadtest.py`: up to 16 GB, 16
workers, steps of 10–1800 s, an hour in all. The agent re-checks every plan
before running it, because its control port is on the network.

---

## Right-sizing: which size to buy

The question a load test is run to answer. `rightsize.py` works it out on the
server from the samples the agents already send, so the dashboard and
`GET /report` give the same answer.

1. **Pressure at each load level.** For each size, the average pressure while
   each level was held, skipping the first 10 s of each step while the 10-second
   reading settles. A level where the kernel killed the test counts as failed:
   once `stress` is killed its memory is freed, and the readings alone would
   make the level look fine.
2. **How much each size holds.** The most memory it held while staying under
   the degraded line. Between the last level it held and the first it did not,
   the crossing is interpolated on the pressure. Pressure rises faster than
   linearly with load, so the estimate errs low.
3. **Room to spare.** Your app's peak plus a margin, 25% unless you change it,
   for growth and for the OS cache. A ramp up past your app's peak measures how
   much room each size really leaves; a peak test can only say it held the peak.
4. **The verdict.** The cheapest size that holds the peak plus the margin, and
   what the alternatives would mean: "t3.small would save $15.18 a month, but it
   holds only about 1.4 GB before slowing down."

The report also says what a usage alarm (memory over 80% in use, the
usual way) would have bought instead. When the two agree, that is said too.
When they differ, it gives the evidence: "t3.small is 88% in use at your app's
peak, yet its programs waited for memory under 1% of the time."

Your app's peak is asked for on the form (for a peak test it is the memory
held), and it and the margin can be changed afterwards on the panel, or with
`?need_mb=` and `?margin=`: no need to run the test again.

**Prices** are AWS's on-demand Linux list prices for the region each agent
reports, from `data/prices.json`. The file records the date it was fetched,
and the report shows it. `python3 tools/update_prices.py` refreshes it from
AWS's public price list, with no AWS account needed. For a discounted rate, a
family it does not cover or another cloud, point `SYSHEALTH_PRICES_FILE` at a
JSON file of your own (`{"t3.micro": 0.0104}`, or by region); it always wins.
Agents off EC2 can set `SYSHEALTH_INSTANCE_TYPE` and `SYSHEALTH_INSTANCE_REGION`.

---

## Why: the likely cause

PSI says how much time programs lost waiting for memory, not why, and the fix
depends on why. Each agent reads the kernel's own evidence (`/proc/meminfo`,
`/proc/vmstat`, `/proc/<pid>/status`) and `diagnose.py` turns it into words:

| Cause | Evidence | What to do |
|---|---|---|
| Swapping | Memory read back from swap, 1 MB/s or more | A size with more RAM; more swap only hides it |
| Programs fill RAM | Recently dropped file pages read back from disk, 1 MB/s or more, with programs holding 70%+ of RAM | A size with more RAM, or make the app hold less |
| Files do not fit | The same re-reading, while programs hold less | More RAM for the cache, or read fewer files at once |
| Reclaim | Programs stopping to free memory before they can get more | If it lasts, more RAM |

A cause is given only while there is pressure to explain. Separately, a process
whose memory has only grown for five minutes or more, by 100 MB and 20% at
least, is called out with the time until the machine would run out at that
rate. The load test's own `stress` is never counted.

Stock EC2 images have no swap, so there the second row is the usual one:
programs fill RAM, and the kernel keeps dropping and re-reading the files they
run from. This project's Terraform gives each agent 512 MB of swap
(`swap_mb`), so on its fleet the first row appears too once that fills.

Each agent also reports CPU and disk I/O pressure, shown beside the memory
breakdown. States go by memory alone; the other two are context, for example
when re-reading files from disk costs more in I/O waits than it shows as
memory pressure.

---

## With Prometheus

The server answers `GET /metrics` in Prometheus's text format (agents keep
`POST`ing to the same path). Point a scrape job at it:

```yaml
scrape_configs:
  - job_name: syshealth
    static_configs:
      - targets: ["YOUR_SERVER_IP:5000"]
```

| Metric | What it is |
|---|---|
| `syshealth_memory_waiting_ratio`, `_minute_ratio`, `syshealth_memory_stalled_ratio` | PSI `some` (10 s and the minute states are judged on) and `full`, as 0–1 |
| `syshealth_state{state=…}` | 1 for the state each machine is in |
| `syshealth_threshold_ratio{line=…}` | Where each state begins, so alerts can use the same lines |
| `syshealth_cpu_waiting_ratio`, `syshealth_io_waiting_ratio`, `syshealth_io_stalled_ratio` | CPU and disk I/O pressure |
| `syshealth_memory_bytes{part=…}`, `syshealth_memory_in_use_ratio`, `syshealth_swap_used_bytes` | Where the RAM is |
| `syshealth_load_test_bytes` | The memory a running test asks for, 0 when none runs |
| `syshealth_capacity_bytes{kind=…}`, `syshealth_recommended`, `syshealth_price_usd_per_hour`, `syshealth_app_need_bytes` | The last finished test's verdict, per size |

Every series carries `hostname`, `instance_type`, `instance_id` and `region`.
Names follow Prometheus conventions (bytes, 0–1 ratios); "waiting" and
"stalled" are PSI's `some` and `full`, as in node_exporter.

---

## Project Structure

```
syshealth/
├── server.py          # Flask central server + REST API
├── syshealth.py       # Agent: collector, analyzer, control server, push client
├── analyzer.py        # Sliding-window PSI classifier, thresholds at the top
├── collector.py       # Reads /proc/pressure/memory (some + full), /proc/vmstat, /proc/meminfo
├── loadtest.py        # Load-test plans: built from the form, checked by the agent
├── rightsize.py       # What a test says each size holds, and which size to buy
├── diagnose.py        # Where the memory is, the likely cause, growing processes
├── prometheus.py      # GET /metrics in Prometheus's text format
├── pricing.py         # What each size costs, from data/prices.json
├── instance.py        # Instance type, id and region via IMDSv2, so sizes can be compared
├── reporter.py        # Local stdout logging
├── dev.py             # Local preview: simulated four-size fleet (npm run dev)
├── deploy.sh          # One-command deploy to all EC2 instances
├── templates/
│   └── index.html     # Dashboard markup
├── static/
│   ├── css/dashboard.css
│   └── js/dashboard.js   # Pressure graph and load-test panel, inline SVG — no libraries
├── data/
│   └── prices.json    # AWS on-demand list prices, dated (tools/update_prices.py)
├── tools/
│   └── update_prices.py  # Refresh data/prices.json from AWS's public price list
├── tests/             # python3 -m unittest discover -s tests  (or npm test)
├── docs/
│   └── thresholds.md  # What the number means; the research behind each threshold
└── terraform/         # Dashboard + one instance per size, in one apply
    └── check.sh       # Which sizes are reporting, and why one is not
```

---

## Requirements

- Python 3.10+
- `pip install flask requests`
- Linux kernel ≥ 4.20 (PSI support)
- `stress` package on agent instances (`sudo apt install stress`)
- Node ≥ 18 only if you want `npm run dev` — `python3 dev.py` is the same thing
- Terraform ≥ 1.3 only if you use `terraform/`

---

## Tech Stack

Python · Flask · Linux PSI · vmstat · AWS EC2 · Terraform · Bash

The graph is hand-written inline SVG with no charting library and no build step,
so the dashboard loads with nothing fetched from a CDN.
