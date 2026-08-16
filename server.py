from collections import OrderedDict, deque
from datetime import datetime
import time

from flask import Flask, jsonify, render_template, request

import instance

app = Flask(__name__)

# In-memory store (fast for demo). Bounded so a long-running server
# cannot grow without limit: 720 samples = ~1 hour at one push per 5s.
MAX_SAMPLES = 720
DEFAULT_HISTORY = 20

# Flat stream, kept for the original /history and /dashboard contracts.
data_store = deque(maxlen=MAX_SAMPLES)

# The same samples, split per reporting machine, so several instances can be
# compared. Keyed by instance id; the dicts are shared with data_store.
instances = OrderedDict()


def instance_key(sample):
    return (
        sample.get("instance_id")
        or sample.get("instance_type")
        or "unknown"
    )


def record(sample):
    key = instance_key(sample)
    entry = instances.get(key)

    if entry is None:
        entry = {
            "id": key,
            "type": sample.get("instance_type") or "unknown",
            "name": sample.get("instance_name") or key,
            "samples": deque(maxlen=MAX_SAMPLES),
        }
        instances[key] = entry
    else:
        # A restarted agent may start reporting richer identity than before.
        if sample.get("instance_type"):
            entry["type"] = sample["instance_type"]
        if sample.get("instance_name"):
            entry["name"] = sample["instance_name"]

    entry["samples"].append(sample)


def sorted_instances():
    """Smallest instance first, so the dashboard's toggle reads as a ladder."""
    return sorted(
        instances.values(),
        key=lambda entry: (instance.size_rank(entry["type"]), entry["type"], entry["id"]),
    )


def clamp_limit(raw):
    try:
        limit = int(raw)
    except (TypeError, ValueError):
        limit = DEFAULT_HISTORY
    return max(1, min(limit, MAX_SAMPLES))


@app.route("/")
def home():
    return render_template("index.html")


@app.route("/healthz")
def healthz():
    return "SysHealth Cloud Server Running"


@app.route("/metrics", methods=["POST"])
def receive_metrics():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "expected a JSON object"}), 400

    data["timestamp"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    # Epoch seconds too, so the dashboard can place samples on a time axis
    # without re-parsing the display string.
    data["received_at"] = time.time()

    data_store.append(data)
    record(data)

    return jsonify({"status": "received"}), 200


@app.route("/dashboard", methods=["GET"])
def dashboard():
    if not data_store:
        return jsonify({"message": "No data yet"})

    latest = data_store[-1]
    return jsonify(latest)


@app.route("/history", methods=["GET"])
def history():
    limit = clamp_limit(request.args.get("limit", DEFAULT_HISTORY))
    wanted = request.args.get("instance")

    if wanted:
        entry = instances.get(wanted)
        samples = list(entry["samples"]) if entry else []
    else:
        samples = list(data_store)

    return jsonify(samples[-limit:])


@app.route("/instances", methods=["GET"])
def list_instances():
    return jsonify([
        {
            "id": entry["id"],
            "type": entry["type"],
            "name": entry["name"],
            "samples": len(entry["samples"]),
            "latest": entry["samples"][-1] if entry["samples"] else None,
        }
        for entry in sorted_instances()
    ])


@app.route("/series", methods=["GET"])
def series():
    """Every instance's recent history in one response — what the dashboard reads."""
    limit = clamp_limit(request.args.get("limit", MAX_SAMPLES))

    return jsonify({
        "instances": [
            {
                "id": entry["id"],
                "type": entry["type"],
                "name": entry["name"],
                "samples": list(entry["samples"])[-limit:],
            }
            for entry in sorted_instances()
        ]
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
