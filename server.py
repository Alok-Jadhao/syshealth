from collections import defaultdict, deque
from datetime import datetime
import time

import requests
from flask import Flask, request, jsonify, render_template

app = Flask(__name__)

HISTORY_LIMIT = 60
ONLINE_WINDOW_SEC = 15
AGENT_CONTROL_PORT = 5001

instances = defaultdict(lambda: {
    "hostname": None,
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

    inst = instances[hostname]
    inst["hostname"] = hostname
    inst["agent_ip"] = request.remote_addr
    inst["last_seen"] = time.time()
    inst["latest"] = data
    inst["history"].append(data)

    return jsonify({"status": "received"}), 200


@app.route("/instances", methods=["GET"])
def list_instances():
    now = time.time()
    result = []
    for host, inst in instances.items():
        online = (now - inst["last_seen"]) <= ONLINE_WINDOW_SEC
        result.append({
            "hostname": host,
            "online": online,
            "last_seen": inst["last_seen"],
            "seconds_since": round(now - inst["last_seen"], 1),
            "latest": inst["latest"],
        })
    result.sort(key=lambda x: x["hostname"])
    return jsonify(result)


@app.route("/instances/<hostname>/history", methods=["GET"])
def instance_history(hostname):
    inst = instances.get(hostname)
    if not inst:
        return jsonify([])
    return jsonify(list(inst["history"]))


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
