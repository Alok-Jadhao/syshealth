from collections import deque
import json
import os

# Where each state begins, as % of time stalled on memory (Linux PSI).
#
# Absolute, not multiples of an idle baseline. PSI is already normalised to
# wall-clock time, so 10% means the same thing on a t3.micro as on a t3.large:
# programs lost one second in every ten waiting for memory. A ratio against an
# idle floor of 0.01 turned 0.1% (no measurable slowdown) into "10x, CRITICAL".
#
# The numbers come from production systems that act on PSI; the sources and the
# reasoning are in docs/thresholds.md.
DEGRADED_SOME = 10.0   # Android lmkd acts at 7%, the kernel docs' example trigger is 15%
CRITICAL_SOME = 40.0   # Meta's oomd example ruleset kills at 40, held for 15s
CRITICAL_FULL = 10.0   # whole machine stalled: warn well before systemd-oomd kills, at 60%

THRESHOLDS = {
    "degraded": DEGRADED_SOME,
    "critical": CRITICAL_SOME,
    "critical_full": CRITICAL_FULL,
}


class Analyzer:
    def __init__(self, window_size=12, persist_count=3, min_samples=6):
        self.psi_history = deque(maxlen=window_size)
        self.full_history = deque(maxlen=window_size)
        self.state = "HEALTHY"
        self.prev_vmstat = None
        self.persist_count = persist_count
        self.min_samples = min_samples
        self.degraded_counter = 0

        # The idle reading from `syshealth.py calibrate`. Reported so a machine's
        # normal floor can be seen, but no longer used to decide the state.
        self.baseline = self.load_baseline()

    def load_baseline(self):
        if os.path.exists("baseline.json"):
            try:
                with open("baseline.json", "r") as f:
                    val = json.load(f)["psi"]
                    return val if val > 0 else 0.01
            except:
                pass
        return 0.01  # fallback

    def update(self, current_psi, current_full, current_vmstat):
        # Store PSI history: one minute at the agent's 5s interval
        self.psi_history.append(current_psi)
        self.full_history.append(current_full)
        avg_psi = sum(self.psi_history) / len(self.psi_history)
        avg_full = sum(self.full_history) / len(self.full_history)

        # VMSTAT DELTA calculation. Reported as a diagnostic only: reclaim
        # activity on its own does not say whether anything is waiting, which
        # is the problem PSI was added to the kernel to solve.
        s_delta, t_delta = 0, 0
        if self.prev_vmstat:
            s_delta = current_vmstat['pgscan_direct'] - self.prev_vmstat['pgscan_direct']
            t_delta = current_vmstat['pgsteal_direct'] - self.prev_vmstat['pgsteal_direct']

        self.prev_vmstat = current_vmstat

        new_state = "HEALTHY"

        # Only analyze after some data collected (~30 sec)
        if len(self.psi_history) < self.min_samples:
            reason = "Collecting the first minute of data (%d of %d samples)" % (
                len(self.psi_history), self.min_samples)
        else:
            if avg_full >= CRITICAL_FULL:
                level = "CRITICAL"
                reason = ("The whole machine was stalled on memory %.1f%% of the "
                          "last minute (critical from %g%%)" % (avg_full, CRITICAL_FULL))
            elif avg_psi >= CRITICAL_SOME:
                level = "CRITICAL"
                reason = ("Programs waited for memory %.1f%% of the last minute "
                          "(critical from %g%%)" % (avg_psi, CRITICAL_SOME))
            elif avg_psi >= DEGRADED_SOME:
                level = "DEGRADED"
                reason = ("Programs waited for memory %.1f%% of the last minute "
                          "(degraded from %g%%)" % (avg_psi, DEGRADED_SOME))
            else:
                level = "HEALTHY"
                if avg_psi < 1:
                    reason = "No measurable wait for memory"
                else:
                    reason = ("Programs waited for memory %.1f%% of the last minute, "
                              "below the %g%% degraded line" % (avg_psi, DEGRADED_SOME))

            if level == "HEALTHY":
                self.degraded_counter = 0
            else:
                # Must hold for persist_count samples (15s) before the state
                # changes, so one spike is not reported as a failure.
                self.degraded_counter += 1
                if self.degraded_counter >= self.persist_count:
                    new_state = level
                else:
                    reason += " - confirming, %d of %d samples" % (
                        self.degraded_counter, self.persist_count)

        self.state = new_state

        return {
            "state": self.state,
            "avg_psi": avg_psi,
            "avg_psi_full": avg_full,
            "pgscan_delta": s_delta,
            "pgsteal_delta": t_delta,
            "reason": reason,
        }
