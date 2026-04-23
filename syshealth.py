import time
import json
import sys
import requests
from collector import Collector
from analyzer import Analyzer
from reporter import Reporter

# Change this to your server IP later
SERVER_URL = "http://<SERVER_IP>:5000/metrics"


def calibrate():
    collector = Collector()
    samples = []

    print("Calibrating baseline... keep system idle for 60 seconds")

    for _ in range(12):  # 12 samples (5 sec each = 60 sec)
        data = collector.get_metrics()
        samples.append(data['psi'])
        time.sleep(5)

    baseline = sum(samples) / len(samples)

    with open("baseline.json", "w") as f:
        json.dump({"psi": baseline}, f)

    print(f"Baseline saved: {baseline:.4f}")


def load_baseline():
    try:
        with open("baseline.json", "r") as f:
            return json.load(f)["psi"]
    except:
        return 0.01  # fallback default


def push_to_server(payload):
    try:
        requests.post(SERVER_URL, json=payload, timeout=2)
    except requests.exceptions.RequestException:
        print("[Cloud] Server not reachable, skipping push...")


def main():
    collector = Collector()
    baseline = load_baseline()

    analyzer = Analyzer(baseline=baseline)
    reporter = Reporter()

    print("SysHealth v2.0 Started...")
    print(f"Using baseline: {baseline:.4f}")
    print("-" * 50)

    try:
        while True:
            data = collector.get_metrics()

            # Updated analyzer returns reason
            state, avg_psi, s_d, t_d, reason = analyzer.update(
                data['psi'], data['vmstat']
            )

            # Local logging
            reporter.log_status(state, data['psi'], avg_psi, s_d, t_d, reason)

            # 🌐 Cloud Push (NEW FEATURE)
            payload = {
                "timestamp": time.time(),
                "state": state,
                "psi": data['psi'],
                "avg_psi": avg_psi,
                "pgscan_delta": s_d,
                "pgsteal_delta": t_d,
                "reason": reason
            }

            push_to_server(payload)

            time.sleep(5)

    except KeyboardInterrupt:
        print("\nSysHealth stopped.")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "calibrate":
        calibrate()
    else:
        main()