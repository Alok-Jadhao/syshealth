from datetime import datetime
import json

class Reporter:
    def __init__(self, log_file="syshealth.log"):
        self.log_file = log_file

    def log_status(self, state, psi, avg_psi, s_d, t_d, reason):
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        log_entry = (
            f"[{timestamp}] {state} | PSI: {psi:.2f} | Avg: {avg_psi:.2f} "
            f"| Reclaim: S:{s_d}/T:{t_d} | Reason: {reason}\n"
        )

        with open(self.log_file, "a") as f:
            f.write(log_entry)

        print(log_entry.strip())

        # EXPORT FOR CLOUD (IMPORTANT)
        self.export_json(state, psi, avg_psi, reason)

    def export_json(self, state, psi, avg_psi, reason):
        data = {
            "state": state,
            "psi": psi,
            "avg_psi": avg_psi,
            "reason": reason
        }

        with open("status.json", "w") as f:
            json.dump(data, f, indent=2)