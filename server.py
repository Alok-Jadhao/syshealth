from collections import defaultdict, deque
from datetime import datetime
import time

import requests
from flask import Flask, request, jsonify, render_template

import instance

app = Flask(__name__)

# One hour of history at one push per 5s, so the dashboard's longest range has
# something to draw.
HISTORY_LIMIT = 720
ONLINE_WINDOW_SEC = 15
AGENT_CONTROL_PORT = 5001

instances = defaultdict(lambda: {
    "hostname": None,
    "instance_type": "unknown",
    "instance_id": None,
    "agent_ip": None,
    "last_seen": 0.0,
    "latest": None,
    "history": deque(maxlen=HISTORY_LIMIT),
})


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

    now = time.time()
    payload = []
    for host, inst in instances.items():
        payload.append({
            "hostname": host,
            "instance_type": inst["instance_type"],
            "instance_id": inst["instance_id"],
            "online": (now - inst["last_seen"]) <= ONLINE_WINDOW_SEC,
            "seconds_since": round(now - inst["last_seen"], 1),
            "samples": list(inst["history"])[-limit:],
        })
    payload.sort(key=by_size)
    return jsonify({"instances": payload})


@app.route("/run-stress", methods=["POST"])
def run_stress():
    now = time.time()
    results = {}
    for host, inst in instances.items():
        if (now - inst["last_seen"]) > ONLINE_WINDOW_SEC:
            results[host] = "offline"
            continue
        agent_ip = inst.get("agent_ip")
        if not agent_ip:
            results[host] = "no ip"
            continue
        try:
            r = requests.post(
                f"http://{agent_ip}:{AGENT_CONTROL_PORT}/stress",
                timeout=4
            )
            results[host] = "started" if r.status_code == 200 else f"err {r.status_code}"
        except Exception as e:
            results[host] = f"unreachable: {e}"
    return jsonify(results)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
