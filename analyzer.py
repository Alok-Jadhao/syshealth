from collections import deque
import json
import os

class Analyzer:
    def __init__(self, window_size=12, persist_count=3):
        self.psi_history = deque(maxlen=window_size)
        self.state = "HEALTHY"
        self.prev_vmstat = None
        self.persist_count = persist_count
        self.degraded_counter = 0

        # Load baseline
        self.baseline = self.load_baseline()

    def load_baseline(self):
        if os.path.exists("baseline.json"):
            try:
                with open("baseline.json", "r") as f:
                    return json.load(f)["psi"]
            except:
                pass
        return 0.01  # fallback

    def update(self, current_psi, current_vmstat):
        # Store PSI history
        self.psi_history.append(current_psi)
        avg_psi = sum(self.psi_history) / len(self.psi_history)

        # VMSTAT DELTA calculation
        s_delta, t_delta = 0, 0
        if self.prev_vmstat:
            s_delta = current_vmstat['pgscan_direct'] - self.prev_vmstat['pgscan_direct']
            t_delta = current_vmstat['pgsteal_direct'] - self.prev_vmstat['pgsteal_direct']

        self.prev_vmstat = current_vmstat

        # Default values
        new_state = "HEALTHY"
        reason = "System stable"

        # Only analyze after some data collected (~30 sec)
        if len(self.psi_history) >= 6:
            ratio = avg_psi / self.baseline if self.baseline > 0 else 0

            # 🔴 CRITICAL condition
            if ratio >= 5 or s_delta > 1000:
                self.degraded_counter += 1
                reason = f"High memory pressure ({ratio:.2f}x baseline)"

                if self.degraded_counter >= self.persist_count:
                    new_state = "CRITICAL"

            # 🟡 DEGRADED condition
            elif ratio >= 2:
                self.degraded_counter += 1
                reason = f"Elevated pressure ({ratio:.2f}x baseline)"

                if self.degraded_counter >= self.persist_count:
                    new_state = "DEGRADED"

            # 🟢 HEALTHY condition
            else:
                self.degraded_counter = 0
                new_state = "HEALTHY"
                reason = "System within normal limits"

        self.state = new_state

        return self.state, avg_psi, s_delta, t_delta, reason