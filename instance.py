"""Work out which machine this agent is running on.

Order of preference:
  1. SYSHEALTH_INSTANCE_* environment variables (always wins, works anywhere)
  2. The EC2 instance metadata service, IMDSv2
  3. The hostname

Everything here is best-effort and fails quiet: off EC2 the metadata service is
unreachable, and the agent must not stall waiting for it.
"""
import os
import socket
import urllib.error
import urllib.request

IMDS_ROOT = "http://169.254.169.254/latest"
IMDS_TIMEOUT = 1.0
TOKEN_TTL = "21600"

# Size ranking for the burstable families, used to order the dashboard toggle.
SIZE_ORDER = [
    "nano", "micro", "small", "medium", "large",
    "xlarge", "2xlarge", "4xlarge", "8xlarge",
]

_cached = None


def _imds(path, token=None):
    """GET one metadata key, or None if this box is not on EC2."""
    request = urllib.request.Request(IMDS_ROOT + path)
    if token:
        request.add_header("X-aws-ec2-metadata-token", token)

    # The metadata service is link-local: never send it through a proxy.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=IMDS_TIMEOUT) as response:
            return response.read().decode("utf-8").strip()
    except (urllib.error.URLError, socket.timeout, OSError, ValueError):
        return None


def _imds_token():
    request = urllib.request.Request(
        IMDS_ROOT + "/api/token",
        method="PUT",
        headers={"X-aws-ec2-metadata-token-ttl-seconds": TOKEN_TTL},
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=IMDS_TIMEOUT) as response:
            return response.read().decode("utf-8").strip()
    except (urllib.error.URLError, socket.timeout, OSError, ValueError):
        return None


def size_rank(instance_type):
    """Position of a type on the size ladder; unknown sizes sort last."""
    if not instance_type or "." not in instance_type:
        return len(SIZE_ORDER)
    size = instance_type.split(".", 1)[1]
    return SIZE_ORDER.index(size) if size in SIZE_ORDER else len(SIZE_ORDER)


def detect(refresh=False):
    """Identify this machine. Cached: the answer cannot change while we run."""
    global _cached
    if _cached is not None and not refresh:
        return _cached

    hostname = socket.gethostname()

    instance_id = os.environ.get("SYSHEALTH_INSTANCE_ID")
    instance_type = os.environ.get("SYSHEALTH_INSTANCE_TYPE")
    name = os.environ.get("SYSHEALTH_INSTANCE_NAME")

    if not instance_id or not instance_type:
        token = _imds_token()
        if token:
            instance_id = instance_id or _imds("/meta-data/instance-id", token)
            instance_type = instance_type or _imds("/meta-data/instance-type", token)

    _cached = {
        "instance_id": instance_id or hostname,
        "instance_type": instance_type or "unknown",
        "instance_name": name or hostname,
    }
    return _cached
