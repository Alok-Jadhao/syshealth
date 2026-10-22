import hmac
import os
import time
import json
import signal
import socket
import subprocess
import sys
import threading
import requests
from http.server import HTTPServer, BaseHTTPRequestHandler
import instance
import loadtest
from collector import AppCgroup, Collector
from analyzer import Analyzer, THRESHOLDS
from diagnose import Diagnoser, app_verdict
from reporter import Reporter

HOSTNAME = socket.gethostname()
# Where samples go: the server's /metrics, e.g. http://10.0.1.5:5000/metrics.
# No default. A built-in address goes stale, and an agent still pointing at one
# would send this machine's readings, and its token, to whoever has that IP now.
SERVER_URL = os.environ.get("SYSHEALTH_SERVER_URL", "").strip() or None
CONTROL_PORT = 5001
# The secret shared with the server: sent with every push, and required of
# anyone asking this machine to run a load test.
TOKEN = os.environ.get("SYSHEALTH_TOKEN", "").strip() or None
# "off" on a machine that should only be watched (your production box, with
# SYSHEALTH_APP set): it reports as usual but never accepts a load test.
CONTROL = os.environ.get("SYSHEALTH_CONTROL", "on").strip().lower() not in ("off", "0", "no", "false")


def _make_killable():
    """Undo the service's OOM protection for the stress child.

    The unit sets OOMScoreAdjust=-500 so the agent outlives the load it
    generates, but oom_score_adj is inherited across fork and exec — which would
    protect `stress` too. On a t3.micro the workload does not fit in RAM plus
    swap, so the kernel must kill something; with both the agent and stress
    shielded it reaches for sshd instead, and the instance is lost rather than
    measured. Raising the value is always permitted, unlike lowering it.
    """
    try:
        with open("/proc/self/oom_score_adj", "w") as f:
            f.write("500")
    except OSError:
        pass


# Extra swap a test asked for: added when it starts, removed when it ends. A
# fixed path, so a file left by a crash is found and removed at the next start.
TEST_SWAP = "/var/lib/syshealth/test-swap"
# Never fill the disk: keep this much free beside the swap file.
SWAP_DISK_SPARE_MB = 1024


def _swaps():
    try:
        with open("/proc/swaps") as f:
            return [line.split()[0] for line in f.readlines()[1:] if line.split()]
    except OSError:
        return []


def add_test_swap(mb, path=TEST_SWAP):
    """Add `mb` of swap for one test. Returns None, or why it could not."""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        st = os.statvfs(os.path.dirname(path))
        free_mb = st.f_bavail * st.f_frsize // (1024 * 1024)
        if free_mb < mb + SWAP_DISK_SPARE_MB:
            return "only %d MB of disk free" % free_mb
        subprocess.run(["fallocate", "-l", "%dM" % mb, path], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=120)
        os.chmod(path, 0o600)
        for argv in (["mkswap", path], ["swapon", path]):
            subprocess.run(argv, check=True, stdout=subprocess.DEVNULL,
                           stderr=subprocess.PIPE, timeout=120)
        return None
    except (OSError, subprocess.SubprocessError) as e:
        remove_test_swap(path)
        detail = getattr(e, "stderr", None)
        return (detail.decode(errors="replace").strip() if detail else str(e)) or type(e).__name__


def remove_test_swap(path=TEST_SWAP):
    """Take the test's swap away again. Its pages come back into RAM first."""
    if not os.path.exists(path):
        return
    if path in _swaps():
        subprocess.run(["swapoff", path], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=900)
        if path in _swaps():
            print(f"test swap {path} is still in use; left in place", flush=True)
            return
    try:
        os.remove(path)
    except OSError:
        pass


def _killpg(proc):
    """End a process started in its own session, and everything it forked."""
    if proc is None or proc.poll() is not None:
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
            proc.wait(timeout=3)
            return
        except subprocess.TimeoutExpired:
            continue
        except ProcessLookupError:
            return


class LoadRunner:
    """Runs one load test at a time: each phase of the plan is one stress run.

    Every phase lasts its full planned time, even if stress dies early. On a
    t3.micro the OOM killer often ends it part way through; if the next phase
    started early, this box would drift out of step with the others and the
    comparison between sizes would fall apart. The early exit is counted
    instead, and reported, because "the test process was killed" is a result.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._proc = None
        self._status = None

    def busy(self):
        return self._thread is not None and self._thread.is_alive()

    def start(self, test_id, plan):
        with self._lock:
            if self.busy():
                return False
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run, args=(test_id, plan), daemon=True)
            self._thread.start()
            return True

    def stop(self):
        was_running = self.busy()
        self._stop.set()
        self._kill()
        return was_running

    def status(self):
        """What the dashboard shows while a test runs; None when idle."""
        status = self._status
        if not status:
            return None
        status = dict(status)
        status["remaining_sec"] = max(0, round(status.pop("ends_at") - time.time()))
        return status

    def _kill(self):
        with self._lock:
            proc = self._proc
        # stress forks its workers; they share its process group.
        _killpg(proc)

    def _run(self, test_id, plan):
        ends_at = time.time() + plan["total_sec"]
        killed = 0
        background = None
        swap_added = 0
        try:
            if plan.get("swap_mb"):
                why_not = add_test_swap(plan["swap_mb"])
                if why_not:
                    print(f"load test {test_id}: no extra swap: {why_not}", flush=True)
                else:
                    swap_added = plan["swap_mb"]
            if plan.get("background_cpu"):
                background = subprocess.Popen(
                    loadtest.background_argv(os.cpu_count() or 1, plan["total_sec"]),
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    start_new_session=True, preexec_fn=_make_killable,
                )
            for i, phase in enumerate(plan["phases"]):
                if self._stop.is_set():
                    break
                phase_end = time.time() + phase["seconds"]
                self._status = {
                    "test_id": test_id,
                    "mode": plan["mode"],
                    "pattern": plan["pattern"],
                    "summary": plan["summary"],
                    "step": i + 1,
                    "steps": len(plan["phases"]),
                    "load_mb": phase["mb"],
                    "stress_killed": killed,
                    "ends_at": ends_at,
                    # What the test added around the load, so the page can say so.
                    "background_cpu": background is not None,
                    "swap_added_mb": swap_added,
                }
                if phase["mb"] > 0:
                    proc = subprocess.Popen(
                        loadtest.stress_argv(plan["workers"], phase["mb"], phase["seconds"]),
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        start_new_session=True,
                        preexec_fn=_make_killable,
                    )
                    with self._lock:
                        self._proc = proc
                    # A stop that landed between Popen and here found no
                    # process to kill.
                    if self._stop.is_set():
                        self._kill()
                    try:
                        code = proc.wait(timeout=phase["seconds"] + 10)
                    except subprocess.TimeoutExpired:
                        self._kill()
                        code = 0
                    if code != 0 and not self._stop.is_set() and time.time() < phase_end - 1:
                        killed += 1
                        self._status["stress_killed"] = killed
                        print(f"load test {test_id}: stress exited early in step {i + 1} "
                              f"(code {code}) - likely the OOM killer", flush=True)
                # Hold the phase for its full time; see the class docstring.
                self._stop.wait(max(0, phase_end - time.time()))
        finally:
            with self._lock:
                self._proc = None
            _killpg(background)
            if swap_added:
                remove_test_swap()
            self._status = None


runner = LoadRunner()


class ControlHandler(BaseHTTPRequestHandler):
    def _reply(self, code, body):
        data = json.dumps(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        body = json.loads(self.rfile.read(min(length, 65536)) or b"{}")
        if not isinstance(body, dict):
            raise ValueError("Expected a JSON object.")
        return body

    def _authorized(self):
        if not TOKEN:
            return True
        scheme, _, value = (self.headers.get("Authorization") or "").partition(" ")
        return scheme.lower() == "bearer" and hmac.compare_digest(value.strip().encode(), TOKEN.encode())

    def do_POST(self):
        # Before reading anything: this port can fill the machine's memory.
        if not self._authorized():
            self._reply(401, {"status": "rejected", "error": "wrong or missing token"})
            return
        if self.path == "/stress":
            try:
                body = self._body()
                # No body is an old server pressing the old button: run the
                # old fixed test. A body without a plan is a mistake, not that.
                if not body:
                    plan = loadtest.build_plan(loadtest.DEFAULT_SPEC)
                elif "plan" in body:
                    plan = loadtest.validate_plan(body["plan"])
                else:
                    raise ValueError("Expected a plan.")
            except (ValueError, AttributeError) as exc:
                self._reply(400, {"status": "rejected", "error": str(exc)})
                return
            test_id = str(body.get("test_id") or int(time.time()))[:32]
            if runner.start(test_id, plan):
                self._reply(200, {"status": "started", "test_id": test_id})
            else:
                self._reply(409, {"status": "busy", "error": "a test is already running"})
        elif self.path == "/stop":
            self._reply(200, {"status": "stopped" if runner.stop() else "idle"})
        else:
            self._reply(404, {"error": "not found"})

    def log_message(self, *args):
        pass


def run_control_server():
    HTTPServer(("0.0.0.0", CONTROL_PORT), ControlHandler).serve_forever()


def calibrate():
    collector = Collector()
    samples = []
    print("Calibrating baseline... keep system idle for 60 seconds")
    for _ in range(12):
        data = collector.get_metrics()
        samples.append(data['psi'])
        time.sleep(5)
    baseline = sum(samples) / len(samples)
    with open("baseline.json", "w") as f:
        json.dump({"psi": baseline}, f)
    print(f"Baseline saved: {baseline:.4f}")


_push_failures = 0


def push_to_server(payload):
    """Send one sample. Never fatal: the agent keeps measuring regardless.

    Failures are counted and reported, because the alternative — silence — is
    what an agent looks like when it cannot reach the dashboard at all. The box
    logs healthy status lines, the dashboard stays empty, and nothing anywhere
    says why. Reported on the first failure and then every 60th (five minutes at
    a 5s interval), so a long outage does not bury the logs.
    """
    global _push_failures
    try:
        r = requests.post(SERVER_URL, json=payload, timeout=2,
                          headers={"Authorization": "Bearer " + TOKEN} if TOKEN else None)
        if r.status_code == 401:
            # Not a network failure, and retrying will not fix it: say what will.
            raise requests.exceptions.RequestException(
                "the server rejected this agent's SYSHEALTH_TOKEN" if TOKEN else
                "the server needs a token; set SYSHEALTH_TOKEN to the server's")
        if r.status_code >= 400:
            # Turned away (422 names the field), so the dashboard did not get it.
            try:
                why = r.json().get("error")
            except (ValueError, AttributeError):
                why = None
            raise requests.exceptions.RequestException(
                "the server turned the sample away (%d): %s" % (r.status_code, why or r.text[:200]))
        if _push_failures:
            print(f"push to {SERVER_URL} recovered after {_push_failures} failures", flush=True)
            _push_failures = 0
    except requests.exceptions.RequestException as exc:
        _push_failures += 1
        if _push_failures == 1 or _push_failures % 60 == 0:
            print(
                f"push to {SERVER_URL} failed ({_push_failures} in a row): {exc}",
                file=sys.stderr, flush=True,
            )


def main():
    if not SERVER_URL:
        sys.exit("SYSHEALTH_SERVER_URL is not set, so there is nowhere to send samples. Point it\n"
                 "at the server: SYSHEALTH_SERVER_URL=http://SERVER_IP:5000/metrics\n"
                 "(http://127.0.0.1:5000/metrics when the server runs on this machine).")
    if CONTROL:
        # Swap a test added before the agent last stopped, without seeing it end.
        remove_test_swap()
        threading.Thread(target=run_control_server, daemon=True).start()

    collector = Collector()
    analyzer = Analyzer()
    diagnoser = Diagnoser()
    # Optional: one app's own memory pressure, from its cgroup.
    app = AppCgroup(os.environ.get("SYSHEALTH_APP")) if os.environ.get("SYSHEALTH_APP") else None
    reporter = Reporter()
    identity = instance.detect()
    mem_total_mb = collector.read_mem_total_mb()

    if not CONTROL:
        print("SysHealth started | load tests off on this machine (SYSHEALTH_CONTROL=off)", flush=True)
    elif TOKEN:
        print(f"SysHealth started | control port {CONTROL_PORT}, token required", flush=True)
    else:
        print(f"SysHealth started | control port {CONTROL_PORT} WITHOUT a token: anything that "
              "can reach it can start a load test. Set SYSHEALTH_TOKEN, or SYSHEALTH_CONTROL=off "
              "on a machine that should only be watched.", flush=True)
    print(f"Instance: {identity['instance_type']} ({HOSTNAME})")
    if app:
        print(f"App: {app.path}" if app.path else
              f"App: no cgroup v2 group found for SYSHEALTH_APP={app.spec!r}; "
              "reporting the machine only", flush=True)
    print("-" * 50)

    try:
        while True:
            data = collector.get_metrics()
            result = analyzer.update(data['psi'], data['psi_full'], data['vmstat'])
            reporter.log_status(result['state'], data['psi'], result['avg_psi'],
                                result['pgscan_delta'], result['pgsteal_delta'],
                                result['reason'], data['psi_full'])

            # Why, as well as how much. The test's own stress process is
            # meant to grow, so it is never reported as a leak.
            why = diagnoser.update(data['vmstat'], data['meminfo'], data['top'],
                                   result, THRESHOLDS, exclude=("stress",))
            app_now = app.read() if app else None
            if app_now:
                app_now["verdict"] = app_verdict(app_now, why["memory"], THRESHOLDS)

            payload = {
                "hostname": HOSTNAME,
                "timestamp": time.time(),
                "psi": data['psi'],
                "psi_full": data['psi_full'],
                # CPU and disk I/O pressure, as context; states go by memory.
                "pressure": data['pressure'],
                # How busy the machine is (CPU and load average), so a box that is
                # working hard but not short of memory does not look idle.
                "cpu": data['cpu'],
                **result,
                # What the dashboard groups, orders and colours machines by.
                "instance_type": identity["instance_type"],
                "instance_id": identity["instance_id"],
                "region": identity["region"],
                "mem_total_mb": mem_total_mb,
                # The lines the state was decided against, so the dashboard
                # draws exactly what this agent used.
                "thresholds": THRESHOLDS,
                "baseline": analyzer.baseline,
                # The load this box is under right now, if a test is running.
                "load_test": runner.status(),
                # False on a watch-only machine, so the server does not ask it.
                "accepts_tests": CONTROL,
                # Where the RAM is (programs, cache, free, swap) and how much
                # of it is "in use" the way most dashboards count it.
                "memory": why["memory"],
                # The likely cause of any pressure, a growing process, and the
                # biggest users. The server keeps it only on the latest sample.
                "diagnosis": {k: why[k] for k in ("cause", "leak", "top", "rates")},
                # One app's own pressure and memory, if SYSHEALTH_APP names it.
                "app": app_now,
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
