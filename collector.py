import os

class Collector:
    def __init__(self):
        self.psi_path = '/proc/pressure/memory'
        self.vmstat_path = '/proc/vmstat'

    def get_metrics(self):
        some, full = self.read_psi()
        return {
            'psi': some,
            'psi_full': full,
            'vmstat': self.read_vmstat()
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

    def read_vmstat(self):
        metrics = {'pgscan_direct': 0, 'pgsteal_direct': 0}
        try:
            with open(self.vmstat_path, 'r') as f:
                for line in f:
                    parts = line.split()
                    if parts[0] in metrics:
                        metrics[parts[0]] = int(parts[1])
            return metrics
        except FileNotFoundError:
            return metrics
