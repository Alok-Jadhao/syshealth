from collections import deque
from datetime import datetime
import time

from flask import Flask, jsonify, render_template, request

app = Flask(__name__)

# In-memory store (fast for demo). Bounded so a long-running server
# cannot grow without limit: 720 samples = ~1 hour at one push per 5s.
MAX_SAMPLES = 720
DEFAULT_HISTORY = 20

data_store = deque(maxlen=MAX_SAMPLES)


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

    return jsonify({"status": "received"}), 200


@app.route("/dashboard", methods=["GET"])
def dashboard():
    if not data_store:
        return jsonify({"message": "No data yet"})

    latest = data_store[-1]
    return jsonify(latest)


@app.route("/history", methods=["GET"])
def history():
    try:
        limit = int(request.args.get("limit", DEFAULT_HISTORY))
    except ValueError:
        limit = DEFAULT_HISTORY

    limit = max(1, min(limit, MAX_SAMPLES))
    return jsonify(list(data_store)[-limit:])


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
