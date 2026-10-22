import os
import time

import diagnose

# /proc/meminfo fields diagnose.breakdown() reads, in kB.
MEMINFO_FIELDS = ("MemTotal", "MemFree", "MemAvailable", "Buffers", "Cached",
                  "AnonPages", "Shmem", "SwapTotal", "SwapFree")


class Collector:
    def __init__(self):
        self.psi_path = '/proc/pressure/memory'
        self.vmstat_path = '/proc/vmstat'
        self.stat_path = '/proc/stat'
        self.loadavg_path = '/proc/loadavg'
        self._cpu_last = None  # (busy, total) jiffies at the last read

    def get_metrics(self):
        some, full = self.read_psi()
        return {
            'psi': some,
            'psi_full': full,
            'pressure': self.read_other_pressure(),
            'cpu': self.read_cpu(),
            'vmstat': self.read_vmstat(),
            'meminfo': self.read_meminfo(),
            'top': diagnose.top_processes(),
        }

    def read_psi(self):
        """avg10 of both PSI lines, as (some, full).

        /proc/pressure/memory looks like:
            some avg10=0.09 avg60=0.57 avg300=0.38 total=1254807180
            full avg10=0.08 avg60=0.49 avg300=0.35 total=1112854986
        "some" is the share of time at least one task waited for memory;
        "full" is the share of time every non-idle task did at once, i.e. the
        machine got no work done. The thresholds use both (docs/thresholds.md).
        """
        values = {'some': 0.0, 'full': 0.0}
        try:
            with open(self.psi_path, 'r') as f:
                for line in f:
                    parts = line.split()
                    if parts and parts[0] in values:
                        # Returns avg10 as a float
                        values[parts[0]] = float(parts[1].split('=')[1])
        except (FileNotFoundError, IndexError, ValueError):
            pass
        return values['some'], values['full']

    def read_other_pressure(self):
        """CPU and disk I/O pressure, avg10, for context beside memory.

        Memory is what the states are judged on. These say whether the box is
        also short of CPU, or whether re-reading files from disk is costing
        more in I/O waits than it shows as memory pressure. CPU `full` is left
        out: at the level of a whole machine it is not meaningful.
        """
        out = {}
        for resource, path in (('cpu', '/proc/pressure/cpu'), ('io', '/proc/pressure/io')):
            try:
                with open(path, 'r') as f:
                    for line in f:
                        parts = line.split()
                        if parts and parts[0] in ('some', 'full'):
                            key = resource if parts[0] == 'some' else resource + '_full'
                            if key != 'cpu_full':
                                out[key] = float(parts[1].split('=')[1])
            except (FileNotFoundError, IndexError, ValueError):
                pass
        return out

    def read_cpu(self):
        """How busy the machine is, beside how short of memory it is.

        A machine can be flat out and still read 0% waiting for memory: a
        model answering requests keeps every vCPU busy while nothing waits for
        RAM. Showing the load says the machine is working, not idle.

            busy_pct  share of CPU time not idle since the last read, across
                      all vCPUs (/proc/stat; iowait counts as idle, steal as
                      busy). Missing on the first read, which has nothing to
                      compare with.
            load1     the 1-minute load average, as `uptime` shows it
            vcpus     so load1 can be read against how many there are
        """
        out = {'vcpus': os.cpu_count() or 1}
        try:
            with open(self.stat_path, 'r') as f:
                fields = f.readline().split()
            # cpu user nice system idle iowait irq softirq steal [guest guest_nice]:
            # guest time is already inside user and nice, so it is left out.
            if fields and fields[0] == 'cpu':
                ticks = [int(v) for v in fields[1:9]]
                total = sum(ticks)
                busy = total - ticks[3] - ticks[4]
                last, self._cpu_last = self._cpu_last, (busy, total)
                if last and total > last[1] and busy >= last[0]:
                    out['busy_pct'] = round(100.0 * (busy - last[0]) / (total - last[1]), 1)
        except (FileNotFoundError, IndexError, ValueError):
            pass
        try:
            with open(self.loadavg_path, 'r') as f:
                out['load1'] = float(f.read().split()[0])
        except (FileNotFoundError, IndexError, ValueError):
            pass
        return out

    def read_mem_total_mb(self):
        """Installed RAM as the kernel sees it, so the dashboard can say how a
        test's load compares to the memory each size actually has."""
        try:
            with open('/proc/meminfo', 'r') as f:
                for line in f:
                    if line.startswith('MemTotal:'):
                        return int(line.split()[1]) // 1024
        except (FileNotFoundError, IndexError, ValueError):
            pass
        return None

    def read_meminfo(self):
        """The /proc/meminfo fields diagnose.py needs, in kB."""
        values = {}
        try:
            with open('/proc/meminfo', 'r') as f:
                for line in f:
                    key, _, rest = line.partition(':')
                    if key in MEMINFO_FIELDS:
                        values[key] = int(rest.split()[0])
        except (FileNotFoundError, IndexError, ValueError):
            pass
        return values

    def read_vmstat(self):
        # pgscan/pgsteal are always present, so the analyser can rely on them;
        # the rest are whatever this kernel has (diagnose.COUNTERS).
        metrics = {'pgscan_direct': 0, 'pgsteal_direct': 0}
        wanted = set(diagnose.COUNTERS)
        try:
            with open(self.vmstat_path, 'r') as f:
                for line in f:
                    parts = line.split()
                    if parts and parts[0] in wanted:
                        metrics[parts[0]] = int(parts[1])
            return metrics
        except (FileNotFoundError, IndexError, ValueError):
            return metrics


CGROUP_ROOT = '/sys/fs/cgroup'


def resolve_cgroup(spec, root=CGROUP_ROOT):
    """The cgroup directory for SYSHEALTH_APP, or None.

    Accepts a systemd unit ("myapp.service", or just "myapp"), a path under
    the cgroup root ("system.slice/docker-<id>.scope"), or a full path. Needs
    cgroup v2, where each group has its own memory.pressure.
    """
    if not spec:
        return None
    spec = spec.strip()
    if spec.startswith('/'):
        candidates = [spec if spec.startswith(root) else root + spec]
    else:
        candidates = [os.path.join(root, spec), os.path.join(root, 'system.slice', spec)]
        if '.' not in spec:
            candidates.append(os.path.join(root, 'system.slice', spec + '.service'))
    for path in candidates:
        if os.path.isfile(os.path.join(path, 'memory.pressure')):
            return path
    return None


class AppCgroup:
    """One app's own memory pressure, from its cgroup rather than the machine.

    The machine's PSI counts every task: the agent, sshd, the OS, and a load
    test. A cgroup's memory.pressure counts only the tasks inside it, so it
    says whether *your app* is waiting. With a memory limit (a container's,
    or systemd's MemoryMax) it also says how close the app runs to it, and
    how often the kernel has killed something inside it for going over.

    Its CPU too, in vCPUs kept busy (cpu.stat, present whether or not the cpu
    controller is on): the figure a burstable size's CPU credits are judged by.
    """

    def __init__(self, spec, root=CGROUP_ROOT):
        self.spec = spec
        self.path = resolve_cgroup(spec, root)
        self.name = spec.strip().rstrip('/').split('/')[-1] if spec else None
        self._cpu = None  # (usage_usec, seconds) at the last read

    def _cpu_vcpu(self, now):
        """vCPUs the app kept busy since the last read, or None the first time."""
        usage = None
        try:
            for line in self._read('cpu.stat').splitlines():
                key, _, value = line.partition(' ')
                if key == 'usage_usec':
                    usage = int(value)
        except (OSError, ValueError):
            return None
        if usage is None:
            return None
        last, self._cpu = self._cpu, (usage, now)
        # A restarted app starts a new count: skip that interval.
        if not last or now <= last[1] or usage < last[0]:
            return None
        return round((usage - last[0]) / 1e6 / (now - last[1]), 3)

    def _read(self, name):
        with open(os.path.join(self.path, name), 'r') as f:
            return f.read()

    def read(self, now=None):
        if not self.path:
            return None
        out = {'name': self.name, 'cpu_vcpu': self._cpu_vcpu(time.monotonic() if now is None else now)}
        try:
            for line in self._read('memory.pressure').splitlines():
                parts = line.split()
                if parts and parts[0] in ('some', 'full'):
                    out['psi' if parts[0] == 'some' else 'psi_full'] = float(parts[1].split('=')[1])
            out['current_mb'] = int(self._read('memory.current')) // (1024 * 1024)
            limit = self._read('memory.max').strip()
            out['limit_mb'] = None if limit == 'max' else int(limit) // (1024 * 1024)
            for line in self._read('memory.stat').splitlines():
                key, _, value = line.partition(' ')
                if key in ('anon', 'file'):
                    out[key + '_mb'] = int(value) // (1024 * 1024)
            for line in self._read('memory.events').splitlines():
                key, _, value = line.partition(' ')
                if key == 'oom_kill':
                    out['oom_kills'] = int(value)
        except (OSError, ValueError, IndexError):
            # The app stopped, or the unit was restarted into a new group.
            self.path = resolve_cgroup(self.spec)
            self._cpu = None
            return None
        return out
