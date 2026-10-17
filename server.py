from collections import OrderedDict, defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import hmac
import os
import time
import uuid

import requests
from flask import Flask, request, jsonify, render_template

import instance
import loadtest
import prometheus
import rightsize

app = Flask(__name__)

# One hour of history at one push per 5s, so the dashboard's longest range has
# something to draw.
HISTORY_LIMIT = 720
ONLINE_WINDOW_SEC = 15
AGENT_CONTROL_PORT = 5001
TESTS_KEPT = 50

# One shared secret for everything that changes state: agents pushing samples,
# and anyone starting or stopping a load test. The server passes it on to the
# agents' control ports. Unset, nothing is checked, and the network (the
# security groups in terraform/) is the only guard.
TOKEN = os.environ.get("SYSHEALTH_TOKEN", "").strip() or None
GUARDED = {("POST", "receive_metrics"), ("POST", "run_stress"), ("POST", "stop_stress")}

instances = defaultdict(lambda: {
    "hostname": None,
    "instance_type": "unknown",
    "instance_id": None,
    "agent_ip": None,
    "last_seen": 0.0,
    "latest": None,
    "history": deque(maxlen=HISTORY_LIMIT),
})

# The plans this server started, by test id, so a report can say what was
# asked for even when the test was stopped part-way. In memory, like the
# history, and only the most recent few.
tests = OrderedDict()


def record_test(test_id, plan):
    tests[test_id] = {"plan": plan, "started": time.time()}
    while len(tests) > TESTS_KEPT:
        tests.popitem(last=False)


def token_ok(header):
    """Whether an Authorization header carries this server's token."""
    if not TOKEN:
        return True
    scheme, _, value = (header or "").partition(" ")
    return scheme.lower() == "bearer" and hmac.compare_digest(value.strip().encode(), TOKEN.encode())


@app.before_request
def require_token():
    if (request.method, request.endpoint) in GUARDED and not token_ok(request.headers.get("Authorization")):
        return jsonify({"error": "This server needs its token (SYSHEALTH_TOKEN) to do that.",
                        "token_required": True}), 401
    return None


@app.route("/")
def home():
    return render_template("index.html")


@app.route("/metrics", methods=["POST"])
def receive_metrics():
    data = request.json or {}
    hostname = data.get("hostname") or "unknown"
    data["received_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    # Epoch seconds as well, so the dashboard can place samples on a time axis
    # without re-parsing the display string or trusting the agent's clock.
    data["received_ts"] = time.time()

    inst = instances[hostname]
    inst["hostname"] = hostname
    inst["agent_ip"] = request.remote_addr
    inst["last_seen"] = time.time()
    inst["latest"] = data
    inst["history"].append(data)

    # An agent that knows what it runs on lets the dashboard group and order
    # machines by size; older agents simply stay "unknown".
    if data.get("instance_type"):
        inst["instance_type"] = data["instance_type"]
    if data.get("instance_id"):
        inst["instance_id"] = data["instance_id"]

    return jsonify({"status": "received"}), 200


@app.route("/metrics", methods=["GET"])
def prometheus_metrics():
    """The same data for Prometheus to scrape (agents POST to this path)."""
    snap = snapshot()
    body = prometheus.render(snap, rightsize.report(snap, plans=tests))
    return body, 200, {"Content-Type": "text/plain; version=0.0.4; charset=utf-8"}


def by_size(entry):
    """Smallest instance first, so the dashboard reads as a size ladder."""
    return (
        instance.size_rank(entry.get("instance_type")),
        entry.get("instance_type") or "",
        entry.get("hostname") or "",
    )


@app.route("/instances", methods=["GET"])
def list_instances():
    now = time.time()
    result = []
    for host, inst in instances.items():
        online = (now - inst["last_seen"]) <= ONLINE_WINDOW_SEC
        result.append({
            "hostname": host,
            "instance_type": inst["instance_type"],
            "instance_id": inst["instance_id"],
            "online": online,
            "last_seen": inst["last_seen"],
            "seconds_since": round(now - inst["last_seen"], 1),
            "latest": inst["latest"],
        })
    result.sort(key=by_size)
    return jsonify(result)


@app.route("/instances/<hostname>/history", methods=["GET"])
def instance_history(hostname):
    inst = instances.get(hostname)
    if not inst:
        return jsonify([])
    return jsonify(list(inst["history"]))


@app.route("/series", methods=["GET"])
def series():
    """Every instance's recent history in one response — what the graph reads."""
    try:
        limit = int(request.args.get("limit", HISTORY_LIMIT))
    except ValueError:
        limit = HISTORY_LIMIT
    limit = max(1, min(limit, HISTORY_LIMIT))
    # ?since= (a received_ts) asks only for what arrived after it: the page
    # fetches the hour once, then only the new samples every 5 s.
    try:
        since = float(request.args["since"]) if request.args.get("since") else None
    except ValueError:
        since = None

    now = time.time()
    payload = []
    for host, inst in list(instances.items()):
        samples = list(inst["history"])[-limit:]
        if since is not None:
            samples = [s for s in samples if (s.get("received_ts") or 0) > since]
        payload.append({
            "hostname": host,
            "instance_type": inst["instance_type"],
            "instance_id": inst["instance_id"],
            "online": (now - inst["last_seen"]) <= ONLINE_WINDOW_SEC,
            "seconds_since": round(now - inst["last_seen"], 1),
            # The diagnosis only ever describes "now", and is the bulk of a
            # sample: the page polls an hour of history every 5 s.
            "samples": [_without(s, "diagnosis") for s in samples[:-1]] + samples[-1:],
        })
    payload.sort(key=by_size)
    return jsonify({"instances": payload, "since": since})


def _without(sample, key):
    if key not in sample:
        return sample
    return {k: v for k, v in sample.items() if k != key}


def snapshot():
    """Every instance and its history, copied, as rightsize.py reads them."""
    now = time.time()
    out = []
    for host, inst in list(instances.items()):
        out.append({
            "hostname": host,
            "instance_type": inst["instance_type"],
            "online": (now - inst["last_seen"]) <= ONLINE_WINDOW_SEC,
            "samples": list(inst["history"]),
        })
    out.sort(key=by_size)
    return out


def _mean(values):
    values = [v for v in values if isinstance(v, (int, float))]
    return round(sum(values) / len(values), 3) if values else None


def _peak(values):
    values = [v for v in values if isinstance(v, (int, float))]
    return max(values) if values else None


def summarise(inst, now):
    """One machine in a few hundred bytes: now, the last hour, and its app."""
    samples = list(inst["history"])
    latest = inst["latest"] or {}
    memory = latest.get("memory") or {}
    diagnosis = latest.get("diagnosis") or {}
    states = {}
    for s in samples:
        states[s.get("state") or "UNKNOWN"] = states.get(s.get("state") or "UNKNOWN", 0) + 1
    out = {
        "hostname": inst["hostname"],
        "instance_type": inst["instance_type"],
        "region": latest.get("region"),
        "online": (now - inst["last_seen"]) <= ONLINE_WINDOW_SEC,
        "seconds_since": round(now - inst["last_seen"], 1),
        "accepts_tests": latest.get("accepts_tests") is not False,
        "now": {
            "state": latest.get("state"),
            "reason": latest.get("reason"),
            "pressure_10s": latest.get("psi"),
            "pressure_minute": latest.get("avg_psi"),
            "full_10s": latest.get("psi_full"),
            "cpu_pressure": (latest.get("pressure") or {}).get("cpu"),
            "io_pressure": (latest.get("pressure") or {}).get("io"),
            "memory": memory or None,
            "cause": diagnosis.get("cause"),
            "leak": diagnosis.get("leak"),
            "top": diagnosis.get("top"),
            "load_test": latest.get("load_test"),
        },
        "hour": {
            "minutes": round((samples[-1].get("received_ts", 0) - samples[0].get("received_ts", 0)) / 60.0, 1)
            if len(samples) > 1 else 0,
            "states": states,
            "worst_minute": _peak([s.get("avg_psi") for s in samples]),
            "peak_used_pct": _peak([(s.get("memory") or {}).get("used_pct") for s in samples]),
        },
        "app": None,
    }
    apps = [s["app"] for s in samples if isinstance(s.get("app"), dict)]
    if apps:
        # What to type into the report: the app's measured peak and average CPU.
        out["app"] = {
            "name": apps[-1].get("name"),
            "now": latest.get("app"),
            "peak_mb": _peak([a.get("current_mb") for a in apps]),
            "peak_anon_mb": _peak([a.get("anon_mb") for a in apps]),
            "avg_cpu_vcpu": _mean([a.get("cpu_vcpu") for a in apps]),
            "peak_cpu_vcpu": _peak([a.get("cpu_vcpu") for a in apps]),
            "worst_pressure": _peak([a.get("psi") for a in apps]),
            "samples": len(apps),
        }
    return out


@app.route("/summary", methods=["GET"])
def summary():
    """Each machine now and over the last hour, small enough to poll."""
    now = time.time()
    rows = [summarise(inst, now) for inst in list(instances.values())]
    rows.sort(key=by_size)
    running = sorted({str(t.get("test_id")) for t in _running_tests(now).values() if t.get("test_id")})
    return jsonify({"instances": rows, "running_tests": running})


def _arg_int(name, low, high, label):
    raw = request.args.get(name, "").strip()
    if not raw:
        return None
    return loadtest._int({name: raw}, name, low, high, label)


@app.route("/report", methods=["GET"])
def test_report():
    """What the last load test (or ?test_id=) says about each size.

    ?need_mb= is the app's memory at its busiest (default: the test's peak),
    ?margin= the share to keep spare on top, in % (default 25), and ?cpu= the
    app's average CPU in vCPUs (optional; 0.5 is half of one vCPU).
    """
    try:
        need = _arg_int("need_mb", 1, loadtest.LIMITS["max_mb"], "Your app's memory")
        margin = _arg_int("margin", 0, rightsize.MAX_MARGIN_PCT, "Room to spare")
        cpu = _arg_cpu()
    except loadtest.PlanError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify(rightsize.report(
        snapshot(), request.args.get("test_id") or None,
        need_mb=need, margin_pct=margin, plans=tests, cpu_vcpu=cpu))


def _arg_cpu():
    raw = request.args.get("cpu", "").strip()
    if not raw:
        return None
    try:
        cpu = float(raw)
    except ValueError:
        cpu = None
    if cpu is None or not 0 < cpu <= rightsize.MAX_CPU_VCPU:
        raise loadtest.PlanError("Your app's CPU must be a number of vCPUs above 0 and up to %d, "
                                 "such as 0.5 for half of one." % rightsize.MAX_CPU_VCPU)
    return cpu


def _plan_from_request():
    """The form's fields as a plan. No body means the old fixed test.

    Only an empty body does: one that fails to parse is an error, not a quiet
    fall-through to 1.6 GB on every instance.
    """
    if not request.get_data():
        return loadtest.build_plan(loadtest.DEFAULT_SPEC)
    spec = request.get_json(silent=True, force=True)
    if spec is None:
        raise loadtest.PlanError("The request body is not valid JSON.")
    return loadtest.build_plan(spec)


@app.route("/load-plan", methods=["POST"])
def load_plan():
    """Preview: what a form submission would run, without running it."""
    try:
        plan = _plan_from_request()
    except loadtest.PlanError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"plan": plan, "commands": loadtest.commands(plan)})


def _running_tests(now):
    """Hosts whose latest sample says a test is still going."""
    busy = {}
    for host, inst in instances.items():
        latest = inst["latest"] or {}
        test = latest.get("load_test")
        if test and (now - inst["last_seen"]) <= ONLINE_WINDOW_SEC:
            busy[host] = test
    return busy


def _online_targets(now, results):
    targets = {}
    for host, inst in instances.items():
        if (now - inst["last_seen"]) > ONLINE_WINDOW_SEC:
            results[host] = "offline"
        elif (inst["latest"] or {}).get("accepts_tests") is False:
            # A machine that is only watched, such as the production box the
            # app's own pressure comes from: never load it.
            results[host] = "skipped: watch only (SYSHEALTH_CONTROL=off)"
        elif not inst.get("agent_ip"):
            results[host] = "no ip"
        else:
            targets[host] = inst["agent_ip"]
    return targets


def _fan_out(targets, send):
    # In parallel, because the comparison is only meaningful if the sizes take
    # the same load at the same moment. Sent one at a time, a single agent that
    # has gone away burns the full timeout before the next is asked, staggering
    # the runs by seconds and pulling the four curves apart for a reason that
    # has nothing to do with instance size.
    if not targets:
        return {}
    with ThreadPoolExecutor(max_workers=len(targets)) as pool:
        futures = {h: pool.submit(send, ip) for h, ip in targets.items()}
    return {h: f.result() for h, f in futures.items()}


def _agent_post(agent_ip, path, body=None):
    try:
        headers = {"Authorization": "Bearer " + TOKEN} if TOKEN else None
        r = requests.post(f"http://{agent_ip}:{AGENT_CONTROL_PORT}{path}", json=body,
                          headers=headers, timeout=4)
        if r.status_code == 401:
            return "rejected: the agent's SYSHEALTH_TOKEN differs from the server's"
        if r.status_code == 404 and path == "/stop":
            return "agent too old to stop a test"
        try:
            reply = r.json()
            if r.ok:
                return reply.get("status") or "ok"
            return "%s: %s" % (reply.get("status") or f"err {r.status_code}",
                               reply.get("error") or "no detail")
        except ValueError:
            # Agents from before load tests answer in plain text.
            return "started (fixed test - agent predates load tests)" if r.ok else f"err {r.status_code}"
    except Exception as e:
        return f"unreachable: {e}"


@app.route("/run-stress", methods=["POST"])
def run_stress():
    try:
        plan = _plan_from_request()
    except loadtest.PlanError as exc:
        return jsonify({"error": str(exc)}), 400

    now = time.time()
    busy = _running_tests(now)
    if busy:
        return jsonify({
            "error": "A test is already running on %s. Stop it first." % ", ".join(sorted(busy)),
        }), 409

    test_id = uuid.uuid4().hex[:8]
    record_test(test_id, plan)
    results = {}
    targets = _online_targets(now, results)
    body = {"test_id": test_id, "plan": plan}
    results.update(_fan_out(targets, lambda ip: _agent_post(ip, "/stress", body)))

    return jsonify({"test_id": test_id, "plan": plan, "results": results})


@app.route("/stop-stress", methods=["POST"])
def stop_stress():
    results = {}
    targets = _online_targets(time.time(), results)
    results.update(_fan_out(targets, lambda ip: _agent_post(ip, "/stop")))
    return jsonify({"results": results})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
