"""An intentionally imperfect service, for chaos testing.

It works fine until told otherwise, and every failure mode is something a real
service does: a slow memory leak, a hot loop, a disk filling up, an error rate
climbing, a process dying. Nothing here simulates a *symptom* — each endpoint
causes a genuine resource problem, so the kernel counters SysHealth reads move
for the real reason.

That distinction matters. A harness that fakes high pressure proves the
dashboard renders. This one proves the detector detects.
"""

from __future__ import annotations

import os
import threading
import time

from flask import Flask, jsonify, request

app = Flask(__name__)

# Held at module scope so the allocation survives the request and the leak is
# a leak rather than a spike.
LEAKED: list[bytearray] = []
STATE = {"burning": False, "latency_ms": 0, "error_rate": 0.0, "started": time.time()}
_lock = threading.Lock()


@app.get("/health")
def health():
    """What a verification step would poll."""
    leaked_mb = sum(len(b) for b in LEAKED) / (1024 * 1024)
    return jsonify(
        {
            "ok": True,
            "uptime_s": round(time.time() - STATE["started"], 1),
            "leaked_mb": round(leaked_mb, 1),
            "burning": STATE["burning"],
            "injected_latency_ms": STATE["latency_ms"],
            "error_rate": STATE["error_rate"],
            "pid": os.getpid(),
        }
    )


@app.get("/")
def index():
    """The normal workload, with whatever faults are currently injected."""
    import random

    if STATE["latency_ms"]:
        time.sleep(STATE["latency_ms"] / 1000.0)
    if STATE["error_rate"] and random.random() < STATE["error_rate"]:
        return jsonify({"error": "injected failure"}), 500
    return jsonify({"ok": True, "served_by": os.getpid()})


# ------------------------------------------------------------- injection ---


@app.post("/chaos/memory-leak")
def memory_leak():
    """Allocate and touch memory, permanently.

    Touched rather than merely allocated: an untouched allocation is not
    resident and produces no pressure at all, which would make this look like
    it worked while measuring nothing.
    """
    mb = int(request.args.get("mb", 64))
    rate_mb_s = int(request.args.get("rate", 32))

    def grow():
        remaining = mb
        while remaining > 0:
            chunk = min(rate_mb_s, remaining)
            block = bytearray(chunk * 1024 * 1024)
            for offset in range(0, len(block), 4096):
                block[offset] = 1
            with _lock:
                LEAKED.append(block)
            remaining -= chunk
            time.sleep(1.0)

    threading.Thread(target=grow, daemon=True).start()
    return jsonify({"leaking_mb": mb, "rate_mb_s": rate_mb_s})


@app.post("/chaos/churn")
def churn():
    """Sustained memory pressure without dying.

    The distinction from ``memory-leak`` matters. Anonymous memory in a
    cgroup with no swap cannot be reclaimed, so growing past the limit is an
    OOM kill, not a stall — the container dies before PSI has much to say.

    File-backed pages *are* reclaimable. Reading a file larger than the
    remaining headroom, over and over, makes the kernel evict pages it is
    about to be asked for again. That is continuous reclaim: real stalling,
    measured as memory pressure, on a container that stays alive. It is also
    the more common production failure of the two.
    """
    seconds = int(request.args.get("seconds", 120))
    size_mb = int(request.args.get("mb", 512))
    path = "/tmp/chaos-churn.dat"

    if not os.path.exists(path) or os.path.getsize(path) < size_mb * 1024 * 1024:
        with open(path, "wb") as handle:
            for _ in range(size_mb):
                handle.write(os.urandom(1024 * 1024))

    def thrash(deadline: float):
        while time.time() < deadline:
            with open(path, "rb") as handle:
                while handle.read(1024 * 1024):
                    if time.time() >= deadline:
                        return

    deadline = time.time() + seconds
    for _ in range(3):
        threading.Thread(target=thrash, args=(deadline,), daemon=True).start()
    return jsonify({"churning_for_s": seconds, "file_mb": size_mb})


@app.post("/chaos/cpu-burn")
def cpu_burn():
    seconds = int(request.args.get("seconds", 60))
    workers = int(request.args.get("workers", os.cpu_count() or 2))
    STATE["burning"] = True

    def burn(deadline: float):
        while time.time() < deadline:
            pow(7, 10_000, 2**61 - 1)
        STATE["burning"] = False

    deadline = time.time() + seconds
    for _ in range(workers):
        threading.Thread(target=burn, args=(deadline,), daemon=True).start()
    return jsonify({"burning_for_s": seconds, "workers": workers})


@app.post("/chaos/disk-fill")
def disk_fill():
    mb = int(request.args.get("mb", 256))
    path = "/tmp/chaos-ballast"
    with open(path, "wb") as handle:
        for _ in range(mb):
            handle.write(b"\0" * 1024 * 1024)
    return jsonify({"wrote_mb": mb, "path": path})


@app.post("/chaos/latency")
def latency():
    STATE["latency_ms"] = int(request.args.get("ms", 500))
    return jsonify({"latency_ms": STATE["latency_ms"]})


@app.post("/chaos/errors")
def errors():
    STATE["error_rate"] = float(request.args.get("rate", 0.5))
    return jsonify({"error_rate": STATE["error_rate"]})


@app.post("/chaos/crash")
def crash():
    """Die abruptly. The supervisor should notice."""
    threading.Timer(0.2, lambda: os._exit(1)).start()
    return jsonify({"crashing": True})


@app.post("/chaos/reset")
def reset():
    """Undo everything. This is what a successful remediation looks like."""
    with _lock:
        LEAKED.clear()
    STATE.update(burning=False, latency_ms=0, error_rate=0.0)
    try:
        os.unlink("/tmp/chaos-ballast")
    except OSError:
        pass
    return jsonify({"reset": True})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, threaded=True)
