"""Load tests: what the dashboard's form asks for, turned into stress runs.

A test is a plan: a list of phases, each one `stress` run holding a set amount
of memory for a set time. Two kinds, picked on the dashboard:

  peak      one phase, the full load for the whole duration. "Can this size
            take my app at its busiest?"
  variable  several phases at different levels, in one of three patterns:
              ramp    low to high in even steps - where does it start to hurt?
              wave    up, then back down - a traffic cycle, and the recovery
              bursts  alternating low and high - short spikes over a base load

Used in three places, which is why it is its own module: server.py builds the
plan from the form, the agent re-checks it before running anything (its control
port is on the network, so it trusts nobody), and dev.py simulates it.
"""

MODES = ("peak", "variable")
PATTERNS = ("ramp", "wave", "bursts")

# Bounds on what a form can ask for. Generous for a t3 fleet, but finite: the
# plan becomes a process on every agent, and one hour is all the history the
# server keeps.
LIMITS = {
    "max_mb": 16384,       # total memory held at once, across workers
    "max_workers": 16,
    "min_phase_sec": 10,   # under this, PSI's 10s average never settles
    "max_phase_sec": 1800,
    "max_total_sec": 3600,
    "min_steps": 2,
    "max_steps": 20,
    "max_phases": 40,
}

# What the old fixed "Stress all" ran, kept as the default so a bare
# `curl -X POST /run-stress` still means the same thing.
DEFAULT_SPEC = {"mode": "peak", "memory_mb": 1600, "workers": 2, "duration_sec": 60}

# need_mb is what the app itself uses at its busiest. The right-sizing report
# judges every size against it. A peak test holds exactly that; a variable
# test usually goes past it, to see how much room each size leaves.


class PlanError(ValueError):
    """A spec or plan that cannot be run. The message is shown to the user."""


def _int(spec, key, low, high, label):
    raw = spec.get(key)
    try:
        # Accept "1600" from a form as well as 1600, but not 1600.5 or "1.6k".
        value = int(raw)
        if isinstance(raw, float) and raw != value:
            raise ValueError
        if isinstance(raw, str) and raw.strip() != str(value):
            raise ValueError
    except (TypeError, ValueError):
        raise PlanError("%s must be a whole number." % label)
    if not low <= value <= high:
        raise PlanError("%s must be between %d and %d." % (label, low, high))
    return value


def _levels(pattern, low, high, steps):
    """The memory level of each phase, in MB, before rounding to workers."""
    if pattern == "ramp":
        return [low + (high - low) * i / (steps - 1) for i in range(steps)]
    if pattern == "wave":
        up = [low + (high - low) * i / (steps - 1) for i in range(steps)]
        return up + up[-2::-1]
    # bursts: a base load, then the spike, `steps` times over
    return [low, high] * steps


def per_worker_mb(mb, workers):
    """stress takes memory per worker; round so the total is what was asked."""
    return max(1, int(round(mb / float(workers))))


def build_plan(spec):
    """Turn a form submission into a validated plan, or raise PlanError."""
    if not isinstance(spec, dict):
        raise PlanError("Expected a JSON object.")

    mode = spec.get("mode", "peak")
    if mode not in MODES:
        raise PlanError("Test type must be one of: %s." % ", ".join(MODES))

    workers = _int(spec, "workers", 1, LIMITS["max_workers"], "Workers")

    if mode == "peak":
        mb = _int(spec, "memory_mb", 1, LIMITS["max_mb"], "Memory")
        seconds = _int(spec, "duration_sec", LIMITS["min_phase_sec"],
                       LIMITS["max_phase_sec"], "Duration")
        phases = [{"mb": mb, "seconds": seconds}]
        pattern = None
        need = mb
    else:
        pattern = spec.get("pattern", "ramp")
        if pattern not in PATTERNS:
            raise PlanError("Pattern must be one of: %s." % ", ".join(PATTERNS))
        low = _int(spec, "low_mb", 0, LIMITS["max_mb"], "Lowest memory")
        high = _int(spec, "high_mb", 1, LIMITS["max_mb"], "Highest memory")
        if high <= low:
            raise PlanError("Highest memory must be more than lowest memory.")
        steps = _int(spec, "steps", LIMITS["min_steps"], LIMITS["max_steps"], "Steps")
        step_sec = _int(spec, "step_sec", LIMITS["min_phase_sec"],
                        LIMITS["max_phase_sec"], "Seconds per step")
        phases = [{"mb": int(round(level)), "seconds": step_sec}
                  for level in _levels(pattern, low, high, steps)]
        need = None
        if spec.get("need_mb") not in (None, ""):
            need = _int(spec, "need_mb", 1, LIMITS["max_mb"], "Your app's memory")

    # The actual total stress will hold: per-worker size times workers.
    for phase in phases:
        if phase["mb"] > 0:
            phase["mb"] = per_worker_mb(phase["mb"], workers) * workers

    plan = {"mode": mode, "pattern": pattern, "workers": workers, "phases": phases,
            "need_mb": need}
    return validate_plan(plan)


def validate_plan(plan):
    """Check a plan that arrived over the network; return a clean copy.

    The agent runs this on whatever the server sends, so nothing outside the
    limits can become a process even if the server is old, wrong or not ours.
    """
    if not isinstance(plan, dict):
        raise PlanError("Plan must be a JSON object.")
    mode = plan.get("mode")
    if mode not in MODES:
        raise PlanError("Unknown test type.")
    pattern = plan.get("pattern")
    if mode == "variable" and pattern not in PATTERNS:
        raise PlanError("Unknown pattern.")
    if mode == "peak":
        pattern = None

    workers = _int(plan, "workers", 1, LIMITS["max_workers"], "Workers")

    raw = plan.get("phases")
    if not isinstance(raw, list) or not raw:
        raise PlanError("Plan has no phases.")
    if len(raw) > LIMITS["max_phases"]:
        raise PlanError("A test can have at most %d phases." % LIMITS["max_phases"])

    phases = []
    for phase in raw:
        if not isinstance(phase, dict):
            raise PlanError("Each phase must be a JSON object.")
        phases.append({
            "mb": _int(phase, "mb", 0, LIMITS["max_mb"], "Phase memory"),
            "seconds": _int(phase, "seconds", LIMITS["min_phase_sec"],
                            LIMITS["max_phase_sec"], "Phase duration"),
        })

    total = sum(p["seconds"] for p in phases)
    if total > LIMITS["max_total_sec"]:
        raise PlanError("The whole test must fit in %d minutes; this one takes %d."
                        % (LIMITS["max_total_sec"] // 60, -(-total // 60)))

    need = None
    if plan.get("need_mb") not in (None, ""):
        need = _int(plan, "need_mb", 1, LIMITS["max_mb"], "Your app's memory")

    return {
        "mode": mode,
        "pattern": pattern,
        "workers": workers,
        "phases": phases,
        "total_sec": total,
        "peak_mb": max(p["mb"] for p in phases),
        "need_mb": need,
        "summary": summarize(mode, pattern, phases),
    }


def stress_argv(workers, mb, seconds):
    """The command one phase runs. A list, never a shell string."""
    return [
        "stress",
        "--vm", str(workers),
        "--vm-bytes", "%dM" % per_worker_mb(mb, workers),
        "--vm-keep",
        "--timeout", "%ds" % seconds,
    ]


def commands(plan):
    """Each phase as the command line it becomes, for the dashboard to show."""
    out = []
    for phase in plan["phases"]:
        if phase["mb"] > 0:
            out.append(" ".join(stress_argv(plan["workers"], phase["mb"], phase["seconds"])))
        else:
            out.append("sleep %d    # no load" % phase["seconds"])
    return out


def format_mb(mb):
    if mb >= 1024 and mb % 1024 == 0:
        return "%d GB" % (mb // 1024)
    if mb >= 1024:
        return "%.1f GB" % (mb / 1024.0)
    return "%d MB" % mb


def summarize(mode, pattern, phases):
    """One line a person can read: what this test does to each machine."""
    if mode == "peak":
        p = phases[0]
        return "Peak: %s held for %d s" % (format_mb(p["mb"]), p["seconds"])
    levels = [p["mb"] for p in phases]
    low, high, each = format_mb(min(levels)), format_mb(max(levels)), phases[0]["seconds"]
    if pattern == "bursts":
        return "Bursts: %s spikes over a %s base, %d times, %d s each" % (
            high, low, len(phases) // 2, each)
    if pattern == "wave":
        return "Up and down: %s to %s and back, %d steps of %d s" % (
            low, high, len(phases), each)
    return "Ramp up: %s to %s in %d steps of %d s" % (low, high, len(phases), each)
