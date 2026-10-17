"""Why a machine is short of memory, said in words.

PSI says how much time programs lost waiting for memory. It does not say why,
and the fix depends on why: a machine whose programs have outgrown its RAM
needs a bigger size; one that keeps re-reading the same files from disk needs
room for its cache; one with a program that only ever grows needs that
program fixed, and a bigger size only buys time.

The kernel keeps the evidence. This turns it into:

  memory  where the RAM is: programs, file cache, the kernel, free, and swap
  top     the processes holding the most
  cause   the likely reason for the pressure, with the numbers behind it,
          only when there is pressure worth explaining
  leak    a process that has grown steadily for several minutes, with when
          it would run the machine out of memory at that rate

Everything is read from /proc by the collector; this module only interprets,
so dev.py's simulated fleet runs the same code as a real agent.
"""

import os
import time
from collections import deque

try:
    PAGE_KB = os.sysconf("SC_PAGE_SIZE") // 1024
except (AttributeError, ValueError, OSError):
    PAGE_KB = 4

# Counters from /proc/vmstat, in pages or events since boot.
COUNTERS = (
    "pgscan_direct", "pgsteal_direct", "pgmajfault", "pswpin", "pswpout",
    "workingset_refault_anon", "workingset_refault_file",
    "workingset_refault",  # kernels before 5.9 have only the combined one
)

# Below these the activity is background noise on any machine, not a cause.
SWAPIN_MB_S = 1.0      # memory coming back from swap
REFAULT_MB_S = 1.0     # recently dropped file pages read back from disk
PROGRAMS_SHARE = 0.7   # programs' own memory, as a share of RAM

# Pressure while this much of RAM is still available is not the machine
# running out: something is held to a memory limit of its own (a container,
# or a systemd unit's MemoryMax). The kernel counts those waits machine-wide
# too, so the machine's pressure alone cannot tell the two apart.
ROOMY_SHARE = 0.25
APP_NEAR_LIMIT = 0.9   # an app at this share of its limit is pressing against it

# A leak: steady growth for at least this long, by at least this much.
LEAK_WINDOW_SEC = 600
LEAK_MIN_SPAN_SEC = 300
LEAK_MIN_MB = 100
LEAK_MIN_SHARE = 0.2
RSS_EVERY_SEC = 60

# Interpreters say little about what they run, so name the script as well.
INTERPRETERS = {"python", "python3", "node", "java", "ruby", "perl", "php", "dotnet", "bun", "deno"}


def mb(kb):
    return int(round(kb / 1024.0))


def breakdown(meminfo):
    """Where the RAM is, in MB, from /proc/meminfo values in kB."""
    total = meminfo.get("MemTotal", 0)
    if not total:
        return None
    free = meminfo.get("MemFree", 0)
    available = meminfo.get("MemAvailable", free)
    programs = meminfo.get("AnonPages", 0)
    shmem = meminfo.get("Shmem", 0)
    # Shared memory and tmpfs sit in "Cached" but cannot be dropped like a
    # file's pages, so they count with the kernel's own use, not the cache.
    cache = max(0, meminfo.get("Cached", 0) + meminfo.get("Buffers", 0) - shmem)
    other = max(0, total - free - programs - cache)
    swap_total = meminfo.get("SwapTotal", 0)
    swap_used = max(0, swap_total - meminfo.get("SwapFree", 0))
    return {
        "total_mb": mb(total),
        "programs_mb": mb(programs),
        "cache_mb": mb(cache),
        "other_mb": mb(other),
        "free_mb": mb(free),
        "available_mb": mb(available),
        # What `free`, CloudWatch-style dashboards and most alarms call used.
        "used_pct": round((total - available) * 100.0 / total, 1),
        "swap_total_mb": mb(swap_total),
        "swap_used_mb": mb(swap_used),
    }


def label(name, cmdline):
    """A process's name, plus the script it runs if it is an interpreter."""
    base = name.split("/")[-1]
    if base.rstrip("0123456789.") in INTERPRETERS or base in INTERPRETERS:
        for arg in cmdline[1:]:
            if arg and not arg.startswith("-"):
                return ("%s %s" % (base, arg.split("/")[-1]))[:40]
    return base[:40]


def top_processes(n=5, proc="/proc"):
    """The n processes holding the most memory: [{pid, name, rss_mb}]."""
    found = []
    try:
        pids = [p for p in os.listdir(proc) if p.isdigit()]
    except OSError:
        return []
    for pid in pids:
        try:
            name, rss = None, None
            with open(os.path.join(proc, pid, "status")) as f:
                for line in f:
                    if line.startswith("Name:"):
                        name = line.split(None, 1)[1].strip()
                    elif line.startswith("VmRSS:"):
                        rss = int(line.split()[1])
                        break
            if not rss or not name:
                continue  # kernel threads have no RSS
            with open(os.path.join(proc, pid, "cmdline"), "rb") as f:
                cmdline = f.read().decode("utf-8", "replace").split("\0")
            found.append({"pid": int(pid), "name": label(name, cmdline), "rss_mb": mb(rss)})
        except (OSError, ValueError, IndexError):
            continue  # it exited while we looked
    found.sort(key=lambda p: p["rss_mb"], reverse=True)
    return found[:n]


def _pages_mb_s(pages_per_s):
    return pages_per_s * PAGE_KB / 1024.0


def classify(rates, memory, avg_psi, avg_full, state, thresholds):
    """The likely cause of the pressure, or None when there is nothing to explain."""
    if memory is None:
        return None
    if state == "HEALTHY" and avg_psi < thresholds["degraded"] and avg_full < thresholds["critical_full"]:
        return None

    swapin = _pages_mb_s(rates.get("pswpin", 0))
    refault = _pages_mb_s(rates.get("workingset_refault_file", 0))
    programs_share = memory["programs_mb"] / float(memory["total_mb"])
    pct = int(round(programs_share * 100))

    if memory["available_mb"] >= ROOMY_SHARE * memory["total_mb"]:
        return {
            "key": "limit",
            "text": "The machine still has %s available, so this is not it running out: "
                    "something on it is most likely held to a memory limit of its own, a "
                    "container or a service with MemoryMax, and is waiting at that limit."
                    % _mb_words(memory["available_mb"]),
            "action": "Find which, and raise its limit. Set SYSHEALTH_APP to watch it directly. "
                      "A bigger instance would not help.",
        }
    if swapin >= SWAPIN_MB_S:
        return {
            "key": "swap",
            "text": "Programs are waiting for memory that was moved out to swap: what they "
                    "use does not fit in RAM. %d MB is in swap, and %.1f MB/s is being read "
                    "back." % (memory["swap_used_mb"], swapin),
            "action": "A size with more RAM. More swap would only hide it.",
        }
    if refault >= REFAULT_MB_S and programs_share >= PROGRAMS_SHARE:
        return {
            "key": "programs",
            "text": "Programs' own memory takes %d%% of RAM, leaving too little for the files "
                    "they run from and read: the kernel keeps dropping those and reading them "
                    "back from disk, %.1f MB/s." % (pct, refault),
            "action": "A size with more RAM, or make the app hold less.",
        }
    if refault >= REFAULT_MB_S:
        return {
            "key": "cache",
            "text": "The files programs read do not fit in the memory left for them: the "
                    "kernel keeps dropping them and reading them back from disk, %.1f MB/s. "
                    "Programs themselves hold only %d%% of RAM." % (refault, pct),
            "action": "More RAM for the cache, or a smaller set of files read at once.",
        }
    if rates.get("pgscan_direct", 0) > 0:
        return {
            "key": "reclaim",
            "text": "Programs are stopping to free memory before they can get more: %d%% of "
                    "RAM is in use, %d%% of it by programs themselves."
                    % (int(round(memory["used_pct"])), pct),
            "action": "If it lasts, a size with more RAM.",
        }
    return {
        "key": "unclear",
        "text": "%d%% of RAM is in use, %d%% by programs. Nothing stands out as the cause: no "
                "swapping, and little re-reading from disk." % (int(round(memory["used_pct"])), pct),
        "action": "",
    }


def _mb_words(value):
    return "%.1f GB" % (value / 1024.0) if value >= 1024 else "%d MB" % value


def app_verdict(app, memory, thresholds):
    """Whether the app is short of memory, and whose fault that is: its own
    limit, a limit on the group it runs in, or the machine's RAM.

    Judged by how much memory the machine has available, not by its pressure:
    the kernel counts the app's waits at its own limit machine-wide as well.
    """
    if not app or app.get("psi") is None or app["psi"] < thresholds["degraded"]:
        return None
    name, psi = app.get("name") or "The app", app["psi"]
    limit, current = app.get("limit_mb"), app.get("current_mb")
    near_limit = bool(limit) and current is not None and current >= APP_NEAR_LIMIT * limit
    roomy = bool(memory) and memory["available_mb"] >= ROOMY_SHARE * memory["total_mb"]
    spare = " (%s available)" % _mb_words(memory["available_mb"]) if memory else ""

    if near_limit and roomy:
        return {
            "key": "app_limit",
            "text": "%s is waiting for memory %.0f%% of the time because it is at its own limit, "
                    "%d of %d MB, while the machine has room to spare%s."
                    % (name, psi, current, limit, spare),
            "action": "Raise the app's memory limit. A bigger instance would not help.",
        }
    if near_limit:
        return {
            "key": "app_limit_and_machine",
            "text": "%s is waiting for memory %.0f%% of the time: it is at its own limit, %d of "
                    "%d MB, and the machine is short of memory as well." % (name, psi, current, limit),
            "action": "Raise the limit, and give the machine more RAM to back it.",
        }
    if roomy:
        return {
            "key": "app_parent",
            "text": "%s is waiting for memory %.0f%% of the time, though it is %s and the "
                    "machine has room%s: a limit on the group it runs in, a pod or a systemd "
                    "slice, is the likely cap."
                    % (name, psi, "under its own limit" if limit else "not limited itself", spare),
            "action": "Check the limits of the slice or pod around it.",
        }
    return {
        "key": "app_machine",
        "text": "%s is waiting for memory %.0f%% of the time because the machine is short of it%s."
                % (name, psi, "" if not limit else ", well before its own %d MB limit" % limit),
        "action": "A size with more RAM, or less else running beside it.",
    }


def _duration(minutes):
    if minutes < 90:
        return "%d minutes" % max(1, int(round(minutes)))
    hours = minutes / 60.0
    return "%.0f hours" % hours if hours < 48 else "%.0f days" % (hours / 24)


class Diagnoser:
    def __init__(self):
        self.prev = None          # (ts, counters)
        self.rss = deque()        # (ts, {pid: (name, rss_mb)}), once a minute
        self.last_rss = 0.0

    def rates(self, vmstat, now):
        """Counter deltas per second since the last call."""
        counters = {k: vmstat[k] for k in COUNTERS if k in vmstat}
        # Before 5.9 the kernel did not split refaults; treat them all as file.
        if "workingset_refault_file" not in counters and "workingset_refault" in counters:
            counters["workingset_refault_file"] = counters["workingset_refault"]
        out = {}
        if self.prev:
            then, before = self.prev
            span = max(1e-6, now - then)
            for key, value in counters.items():
                if key in before:
                    out[key] = round(max(0, value - before[key]) / span, 1)
        self.prev = (now, counters)
        return out

    def leak(self, top, memory, now, exclude=()):
        """A process that has only grown for several minutes, or None."""
        if now - self.last_rss >= RSS_EVERY_SEC:
            self.last_rss = now
            self.rss.append((now, {p["pid"]: (p["name"], p["rss_mb"]) for p in top}))
            while self.rss and now - self.rss[0][0] > LEAK_WINDOW_SEC:
                self.rss.popleft()
        if len(self.rss) < 3 or now - self.rss[0][0] < LEAK_MIN_SPAN_SEC - RSS_EVERY_SEC:
            return None

        worst = None
        for pid, (name, last) in self.rss[-1][1].items():
            if name.split()[0] in exclude:
                continue
            series = [(ts, seen[pid][1]) for ts, seen in self.rss if pid in seen]
            if len(series) < 3 or series[-1][0] - series[0][0] < LEAK_MIN_SPAN_SEC - RSS_EVERY_SEC:
                continue
            first = series[0][1]
            grew = last - first
            steps = [b[1] - a[1] for a, b in zip(series, series[1:])]
            # Steady: it has given back almost nothing on the way up.
            steady = sum(1 for d in steps if d >= -2) >= 0.8 * len(steps)
            if not steady or grew < max(LEAK_MIN_MB, LEAK_MIN_SHARE * first):
                continue
            minutes = (series[-1][0] - series[0][0]) / 60.0
            rate = grew / minutes
            if worst is None or rate > worst["mb_per_min"]:
                worst = {"pid": pid, "name": name, "from_mb": first, "to_mb": last,
                         "minutes": round(minutes, 1), "mb_per_min": round(rate, 1)}
        if worst is None:
            return None
        text = "%s has grown from %d MB to %d MB in %s, and has given none of it back." % (
            worst["name"], worst["from_mb"], worst["to_mb"], _duration(worst["minutes"]))
        if memory and memory.get("available_mb") is not None and worst["mb_per_min"] > 0:
            eta = memory["available_mb"] / worst["mb_per_min"]
            worst["runs_out_min"] = round(eta)
            text += " At that rate the machine runs out of memory in about %s." % _duration(eta)
        worst["text"] = text
        return worst

    def update(self, vmstat, meminfo, top, analysis, thresholds, now=None, exclude=()):
        """Everything above for one sample. `analysis` is Analyzer.update()'s result."""
        now = time.time() if now is None else now
        memory = breakdown(meminfo)
        rates = self.rates(vmstat, now)
        return {
            "memory": memory,
            "rates": rates,
            "top": top[:3],
            "cause": classify(rates, memory, analysis["avg_psi"], analysis["avg_psi_full"],
                              analysis["state"], thresholds),
            "leak": self.leak(top, memory, now, exclude),
        }
