#!/usr/bin/env python3
"""Local preview: the dashboard with a simulated four-instance fleet.

`npm run dev` runs this. It exists because the dashboard only says anything
once several differently-sized machines are pushing, and standing up four EC2
instances to look at a chart is a poor trade.

The PSI numbers here are SYNTHETIC. Everything downstream of them is not: each
simulated machine is driven through the real Analyzer, so the states, the
reasons and the threshold lines come from the same code that runs in
production, and load tests are planned by the same loadtest.py the agents use.
Only the sensor is faked.

Every page served this way carries a DEMO banner. A fabricated graph that
looks real is worse than no graph at all.
"""

import math
import random
import threading
import time
import uuid
from datetime import datetime

try:
    from flask import jsonify

    import loadtest
    import server
    from analyzer import Analyzer, THRESHOLDS
except ModuleNotFoundError as exc:
    # Ubuntu refuses `pip install` into the system Python (PEP 668), so the
    # obvious next move fails too. Say what does work instead of leaving a
    # traceback and an "externally-managed-environment" error to connect.
    if exc.name not in ("flask", "requests"):
        raise
    raise SystemExit(
        "\n%s is not installed.\n\n"
        "On Debian or Ubuntu, install the packaged versions:\n"
        "    sudo apt install python3-flask python3-requests\n\n"
        "Or keep them out of the system Python with a virtualenv:\n"
        "    python3 -m venv .venv\n"
        "    .venv/bin/pip install flask requests\n"
        "    source .venv/bin/activate    # then: npm run dev\n\n"
        "Plain `pip install flask` will fail with "
        "\"externally-managed-environment\" on these systems.\n" % exc.name
    )

PORT = 5000
TICK_SEC = 5.0          # what the real agent pushes at
BACKFILL_SEC = 60 * 60  # fill the server's whole buffer, so "1h" has a shape

# One profile per size. RAM is the point of the whole comparison: the same
# memory demand that a 8 GiB t3.large does not notice leaves a 1 GiB t3.micro
# stalled in reclaim most of the time. Baselines differ too - a micro idles
# noisier than a large.
PROFILES = [
    # type,         RAM MB, baseline, idle
    ("t3.micro",    1024,   0.09,     0.10),
    ("t3.small",    2048,   0.05,     0.06),
    ("t3.medium",   4096,   0.02,     0.03),
    ("t3.large",    8192,   0.012,    0.015),
]

# What Ubuntu, the agent and the page cache it cannot give back hold on to.
OS_OVERHEAD_MB = 350

# A background story for when nobody is running a test: idle, a mild spell, idle,
# one load episode, recovery. A 15-minute cycle, so the default 15m range shows
# it once. The mild spell is sized to bother only the smallest box, the episode
# is the old fixed test (1.6 GB) - so the sizes separate the way they do on AWS.
CYCLE_SEC = 900.0
MILD_START = 220.0
MILD_END = 430.0
MILD_MB = 650
STRESS_START = 600.0
STRESS_RAMP = 20.0
STRESS_HOLD = 60.0
STRESS_DECAY = 30.0
STRESS_MB = 1600

# After a test the background cycle stays quiet for a while, so the test's
# result is not muddied by a simulated episode starting right behind it.
QUIET_AFTER_TEST = 180.0

# On a real t3.micro the OOM killer ends a stress run that does not fit. Past
# this multiple of free memory, the simulated one is killed too.
OOM_RATIO = 2.0
OOM_AFTER_SEC = 25.0


class FakeMachine:
    """One simulated instance: synthetic PSI in, real analyser out."""

    def __init__(self, instance_type, ram_mb, baseline, idle, index):
        self.instance_type = instance_type
        self.instance_id = "i-demo%011x" % (0xd0 + index)
        self.hostname = "demo-" + instance_type.replace(".", "-")
        self.headroom_mb = ram_mb - OS_OVERHEAD_MB
        # What MemTotal reports: a little under the nominal size, as on EC2.
        self.mem_total_mb = int(ram_mb * 0.93)
        self.idle = idle

        self.analyzer = Analyzer()
        # Bypass baseline.json: each machine gets the baseline its own idle
        # calibration would have produced.
        self.analyzer.baseline = baseline

        self.pgscan = 1000 * (index + 1)
        self.pgsteal = 900 * (index + 1)
        self.rng = random.Random(1234 + index)

        # A load test started from the dashboard: {"id", "plan", "started"}.
        self.test = None
        self.killed_phases = set()
        self.quiet_until = 0.0

    # ---- load ----

    def start_test(self, test_id, plan, now):
        self.test = {"id": test_id, "plan": plan, "started": now}
        self.killed_phases = set()
        self.quiet_until = now + plan["total_sec"] + QUIET_AFTER_TEST

    def stop_test(self, now):
        was_running = self.test_phase(now) is not None
        self.test = None
        self.quiet_until = now + QUIET_AFTER_TEST
        return was_running

    def test_phase(self, ts, test=None):
        """(index, phase, seconds into it) of the running test, or None."""
        test = test or self.test
        if not test:
            return None
        into = ts - test["started"]
        if into < 0:
            return None
        for i, phase in enumerate(test["plan"]["phases"]):
            if into < phase["seconds"]:
                return i, phase, into
            into -= phase["seconds"]
        return None

    def cycle_demand(self, ts):
        """MB the background story asks for at `ts`, when no test is running."""
        if ts < self.quiet_until:
            return 0.0
        phase = ts % CYCLE_SEC

        if MILD_START <= phase < MILD_END:
            # Half a sine over the window: rises, peaks in the middle, falls.
            into = (phase - MILD_START) / (MILD_END - MILD_START)
            return MILD_MB * math.sin(into * math.pi)

        if phase < STRESS_START:
            return 0.0
        into = phase - STRESS_START
        if into < STRESS_RAMP:
            return STRESS_MB * into / STRESS_RAMP
        if into < STRESS_RAMP + STRESS_HOLD:
            return STRESS_MB
        # Reclaim keeps stalling for a while after the load stops.
        decay = (into - STRESS_RAMP - STRESS_HOLD) / STRESS_DECAY
        return STRESS_MB * max(0.0, 1.0 - decay)

    def pressure_for(self, demand_mb):
        """Synthetic (some, full) PSI for holding `demand_mb` on this machine.

        Nothing until demand nears what is free, then steeply up, saturating
        once it is well past it - stalling is not linear in demand. "full"
        (every task waiting at once) only appears once demand overruns RAM.
        """
        if demand_mb <= 0:
            return self.idle, 0.0
        ratio = demand_mb / self.headroom_mb
        x = min(1.0, max(0.0, ratio - 0.6))
        some = self.idle + 75.0 * x ** 1.6
        full = some * min(0.85, max(0.0, (ratio - 0.95) / 1.5))
        return some, full

    # ---- one sample ----

    def sample(self, ts):
        """Build the payload the real agent would push at time `ts`."""
        status = None
        # One read: a request thread may start or stop a test mid-sample.
        test = self.test
        current = self.test_phase(ts, test) if test else None
        if current:
            i, phase, into = current
            demand = phase["mb"]
            # Too big to fit: the kernel kills stress, and the demand - and
            # with it the pressure - collapses for the rest of the step.
            if demand / self.headroom_mb >= OOM_RATIO and into >= OOM_AFTER_SEC:
                self.killed_phases.add(i)
            if i in self.killed_phases:
                demand = 0
            plan = test["plan"]
            status = {
                "test_id": test["id"],
                "mode": plan["mode"],
                "pattern": plan["pattern"],
                "summary": plan["summary"],
                "step": i + 1,
                "steps": len(plan["phases"]),
                "load_mb": phase["mb"],
                "stress_killed": len(self.killed_phases),
                "remaining_sec": max(0, round(
                    test["started"] + plan["total_sec"] - ts)),
            }
        else:
            demand = self.cycle_demand(ts)

        some, full = self.pressure_for(demand)
        some = max(0.0, some * self.rng.uniform(0.82, 1.18))
        full = min(some, max(0.0, full * self.rng.uniform(0.82, 1.18)))

        # Tie reclaim to the stall figure rather than to the load knob: tasks
        # stall *because* the kernel is scanning, so the two move together.
        # Scans outnumber successful steals.
        scans = int(some * self.rng.uniform(8, 22))
        self.pgscan += scans
        self.pgsteal += int(scans * self.rng.uniform(0.55, 0.85))

        # Two decimal places, because that is all /proc/pressure/memory gives
        # the real collector.
        some = round(some, 2)
        full = round(full, 2)

        result = self.analyzer.update(
            some, full,
            {"pgscan_direct": self.pgscan, "pgsteal_direct": self.pgsteal},
        )

        payload = {
            "hostname": self.hostname,
            "timestamp": ts,
            "psi": some,
            "psi_full": full,
            "instance_type": self.instance_type,
            "instance_id": self.instance_id,
            "mem_total_mb": self.mem_total_mb,
            "thresholds": THRESHOLDS,
            "baseline": self.analyzer.baseline,
            "load_test": status,
            "demo": True,
        }
        payload.update(result)
        payload["avg_psi"] = round(payload["avg_psi"], 4)
        payload["avg_psi_full"] = round(payload["avg_psi_full"], 4)
        return payload


def ingest(machine, payload, ts):
    """Store a sample as if it had arrived over POST /metrics at time `ts`.

    The real endpoint stamps arrival from the wall clock, which is right for
    an agent and useless for a seeder that has to place samples in the past —
    hence the explicit timestamp here.
    """
    payload = dict(payload)
    payload["received_ts"] = ts
    payload["received_at"] = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")

    inst = server.instances[machine.hostname]
    inst["hostname"] = machine.hostname
    inst["instance_type"] = machine.instance_type
    inst["instance_id"] = machine.instance_id
    inst["agent_ip"] = "127.0.0.1"
    inst["last_seen"] = ts
    inst["latest"] = payload
    inst["history"].append(payload)


def backfill(machines, now):
    """An hour of history, so the page has something on it immediately."""
    start = now - BACKFILL_SEC
    ticks = int(BACKFILL_SEC / TICK_SEC)
    for i in range(ticks):
        ts = start + i * TICK_SEC
        for machine in machines:
            ingest(machine, machine.sample(ts), ts)


def push_forever(machines):
    while True:
        now = time.time()
        for machine in machines:
            ingest(machine, machine.sample(now), now)
        time.sleep(TICK_SEC)


def install_demo_routes(machines):
    @server.app.route("/dev-mode")
    def dev_mode():
        return jsonify({"demo": True, "instances": len(machines)})

    # Replace the real /run-stress and /stop-stress, which would try to reach
    # agents that do not exist, with ones that drive the simulation. The plan
    # is built and checked by the same code as in production.
    def demo_run_stress():
        try:
            plan = server._plan_from_request()
        except loadtest.PlanError as exc:
            return jsonify({"error": str(exc)}), 400
        now = time.time()
        if any(m.test_phase(now) for m in machines):
            return jsonify({"error": "A test is already running on the simulated "
                                     "fleet. Stop it first."}), 409
        test_id = uuid.uuid4().hex[:8]
        for machine in machines:
            machine.start_test(test_id, plan, now)
        return jsonify({
            "test_id": test_id,
            "plan": plan,
            "results": {m.hostname: "started (simulated)" for m in machines},
        })

    def demo_stop_stress():
        now = time.time()
        return jsonify({"results": {
            m.hostname: "stopped (simulated)" if m.stop_test(now) else "idle"
            for m in machines
        }})

    server.app.view_functions["run_stress"] = demo_run_stress
    server.app.view_functions["stop_stress"] = demo_stop_stress


def main():
    machines = [
        FakeMachine(t, ram, base, idle, i)
        for i, (t, ram, base, idle) in enumerate(PROFILES)
    ]

    now = time.time()
    backfill(machines, now)
    install_demo_routes(machines)
    threading.Thread(target=push_forever, args=(machines,), daemon=True).start()

    print("=" * 66)
    print(" SysHealth dashboard — DEMO MODE")
    print("=" * 66)
    print(" The PSI values are simulated. Nothing here is a measurement of")
    print(" this machine or of any EC2 instance.")
    print()
    for machine in machines:
        some, _ = machine.pressure_for(STRESS_MB)
        print("   %-11s free ~%4d MB   under %s: ~%.1f%% stalled"
              % (machine.instance_type, machine.headroom_mb,
                 loadtest.format_mb(STRESS_MB), some))
    print()
    print(" Open http://127.0.0.1:%d" % PORT)
    print(" Use the Instance toggle to compare sizes; 'Load test' runs a")
    print(" simulated peak or variable test. Ctrl-C to stop.")
    print("=" * 66)

    # No reloader: it would fork a second process, and the seeded history lives
    # in this one's memory.
    server.app.run(host="127.0.0.1", port=PORT, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
