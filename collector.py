import os

class Collector:
    def __init__(self):
        self.psi_path = '/proc/pressure/memory'
        self.vmstat_path = '/proc/vmstat'

    def get_metrics(self):
        return {
            'psi': self.read_psi(),
            'vmstat': self.read_vmstat()
        }

    def read_psi(self):
        try:
            with open(self.psi_path, 'r') as f:
                line = f.readline().split()
                # Returns avg10 as a float
                return float(line[1].split('=')[1])
        except (FileNotFoundError, IndexError, ValueError):
            return 0.0

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