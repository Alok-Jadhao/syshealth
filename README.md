# SysHealth — Real-Time Cloud Instance Health Monitor

A lightweight, self-hosted framework for monitoring memory health across AWS EC2 instances in real time. Uses Linux PSI (Pressure Stall Information) kernel metrics, a Flask central server, and a live web dashboard with per-instance health cards and time-series charts.

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
- Reads `/proc/pressure/memory` (PSI avg10) and `/proc/vmstat` every 5 seconds
- Classifies health as **HEALTHY / DEGRADED / CRITICAL** using a calibrated baseline
- POSTs a JSON payload to the central server
- Runs a control server on port 5001 that accepts stress test commands from the dashboard

---

## Dashboard

Open `http://YOUR_SERVER_IP:5000` in your browser.

- Live cards per instance — green dot (online) / grey dot (offline)
- Health badge: HEALTHY / DEGRADED / CRITICAL
- PSI, Avg PSI, pgscan/pgsteal deltas, last-seen timestamp
- Chart.js time-series PSI chart per instance (auto-updates every 3s)
- **⚡ Stress All** button — fires simultaneous memory stress tests on all online instances

---

## Experimental Results

Same stress test (`stress --vm 2 --vm-bytes 800M --vm-keep`) run on all four instance types simultaneously:

| Instance Type | RAM | Peak PSI | State |
|---|---|---|---|
| t3.micro | 1 GB | 17.10 | **CRITICAL** |
| t3.small | 2 GB | 11.05 | **CRITICAL** |
| t3.medium | 4 GB | 0.00 | **HEALTHY** |
| t3.large | 8 GB | 0.00 | **HEALTHY** |

**Finding:** t3.medium (4 GB RAM) is the minimum instance type that stays HEALTHY under a 1.6 GB memory workload.

---

## Quick Start

### 1. Launch EC2 instances (AWS CLI)

```bash
# Create key pair
aws ec2 create-key-pair --region eu-north-1 --key-name syshealth-key \
  --query "KeyMaterial" --output text > syshealth-key.pem
chmod 400 syshealth-key.pem

# Create security group (ports 22, 5000, 5001)
SG_ID=$(aws ec2 create-security-group --region eu-north-1 \
  --group-name syshealth-sg --description "SysHealth" \
  --query "GroupId" --output text)

aws ec2 authorize-security-group-ingress --region eu-north-1 \
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

| State | Condition | Persistence |
|---|---|---|
| HEALTHY | PSI ratio < 2× baseline | Immediate |
| DEGRADED | PSI ratio 2–5× baseline | 3 consecutive samples (15s) |
| CRITICAL | PSI ratio ≥ 5× baseline or pgscan delta > 1000 | 3 consecutive samples (15s) |

**Baseline calibration:** Each agent runs a 60-second idle measurement on first launch and saves the average PSI to `baseline.json`. This accounts for per-instance hardware variation. Instances with zero idle PSI fall back to a baseline of 0.01.

---

## REST API

| Method | Endpoint | Description |
|---|---|---|
| GET | `/` | Live dashboard |
| POST | `/metrics` | Agent metric push |
| GET | `/instances` | All instances with online status and latest metrics |
| GET | `/instances/<hostname>/history` | Last 60 samples for one instance |
| POST | `/run-stress` | Trigger stress test on all online agents |

```bash
# List all instances
curl http://YOUR_SERVER_IP:5000/instances

# Trigger stress test on all instances
curl -X POST http://YOUR_SERVER_IP:5000/run-stress
```

---

## Customising the Stress Test

Edit the stress parameters in `syshealth.py`:

```python
["stress", "--vm", "2", "--vm-bytes", "800M", "--vm-keep", "--timeout", "120s"]
```

| Flag | Effect |
|---|---|
| `--vm 2` | Number of memory worker processes |
| `--vm-bytes 800M` | RAM each worker allocates |
| `--timeout 120s` | Duration of the stress test |

Then run `./deploy.sh agents` to push the change to all instances.

---

## Project Structure

```
syshealth/
├── server.py          # Flask central server + REST API
├── syshealth.py       # Agent: collector, analyzer, control server, push client
├── analyzer.py        # Sliding-window PSI classifier (HEALTHY/DEGRADED/CRITICAL)
├── collector.py       # Reads /proc/pressure/memory and /proc/vmstat
├── reporter.py        # Local stdout logging
├── deploy.sh          # One-command deploy to all EC2 instances
└── templates/
    └── index.html     # Real-time dashboard (HTML + Chart.js)
```

---

## Requirements

- Python 3.10+
- `pip install flask requests`
- Linux kernel ≥ 4.20 (PSI support)
- `stress` package on agent instances (`sudo apt install stress`)

---

## Tech Stack

Python · Flask · Chart.js · Linux PSI · vmstat · AWS EC2 · AWS CLI · Bash
