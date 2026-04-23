from flask import Flask, request, jsonify
from datetime import datetime

app = Flask(__name__)

# In-memory store (fast for demo)
data_store = []

@app.route("/")
def home():
    return "SysHealth Cloud Server Running"

@app.route("/metrics", methods=["POST"])
def receive_metrics():
    data = request.json
    data["timestamp"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
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
    return jsonify(data_store[-20:])  # last 20 entries

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)