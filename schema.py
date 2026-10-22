"""What a pushed sample may contain, so that one bad push cannot break the pages.

The server keeps samples as they arrive, and every page, the report and
/metrics are built from them. A value of the wrong type used to be stored as
it came: a number for instance_type, and /instances, /series, /summary,
/report and /metrics all failed; a string for memory, and they kept failing
for the hour it stayed in the history. So each field the agent sends is
checked here before anything is kept, and a bad one is turned away with the
field's name.

Every field but hostname may be missing or null, since older agents send
fewer. Fields this table does not know pass, so a newer agent can still talk
to an older server; only their numbers are checked, as NaN or Infinity
anywhere would make the pages invalid JSON. dev.py builds the same samples,
and tests/test_ingest.py checks that this table accepts all of them.
"""

import math

# A real sample is about 3 KB.
MAX_BODY_BYTES = 256 * 1024
MAX_NUMBER = 1e15
MAX_NAME = 255
MAX_TEXT = 2000
MAX_LIST = 50
MAX_DEPTH = 12


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "should be a number"
    if not abs(value) <= MAX_NUMBER:
        return "is out of range"
    return None


def _count(value):
    if isinstance(value, bool) or not isinstance(value, int):
        return "should be a whole number"
    if not 0 <= value <= MAX_NUMBER:
        return "is out of range"
    return None


def _text(limit):
    def check(value):
        if not isinstance(value, str):
            return "should be text"
        if len(value) > limit:
            return "is longer than %d characters" % limit
        return None
    return check


def _flag(value):
    return None if isinstance(value, bool) else "should be true or false"


def _object(value):
    return None if isinstance(value, dict) else "should be an object"


def _list(value):
    if not isinstance(value, list):
        return "should be a list"
    if len(value) > MAX_LIST:
        return "has more than %d items" % MAX_LIST
    return None


NUMBER, COUNT, NAME, TEXT = _number, _count, _text(MAX_NAME), _text(MAX_TEXT)
FLAG, OBJECT, LIST = _flag, _object, _list

# Field path -> check. "[]" is any item of a list, and "*" any key of an object
# whose values are all numbers (a new kind of pressure or memory is a number too).
FIELDS = {
    "hostname": NAME,
    "timestamp": NUMBER,
    "psi": NUMBER,
    "psi_full": NUMBER,
    "avg_psi": NUMBER,
    "avg_psi_full": NUMBER,
    "baseline": NUMBER,
    "state": NAME,
    "reason": TEXT,
    "pgscan_delta": NUMBER,
    "pgsteal_delta": NUMBER,
    "pressure": OBJECT,
    "pressure.*": NUMBER,
    "cpu": OBJECT,
    "cpu.busy_pct": NUMBER,
    "cpu.load1": NUMBER,
    "cpu.vcpus": COUNT,
    "instance_type": NAME,
    "instance_id": NAME,
    "region": NAME,
    "mem_total_mb": NUMBER,
    "thresholds": OBJECT,
    "thresholds.*": NUMBER,
    "accepts_tests": FLAG,
    "demo": FLAG,
    "load_test": OBJECT,
    "load_test.test_id": NAME,
    "load_test.mode": NAME,
    "load_test.pattern": NAME,
    "load_test.summary": TEXT,
    "load_test.step": COUNT,
    "load_test.steps": COUNT,
    "load_test.load_mb": NUMBER,
    "load_test.stress_killed": COUNT,
    "load_test.remaining_sec": NUMBER,
    "load_test.background_cpu": FLAG,
    "load_test.swap_added_mb": COUNT,
    "memory": OBJECT,
    "memory.*": NUMBER,
    "diagnosis": OBJECT,
    "diagnosis.cause": OBJECT,
    "diagnosis.cause.key": NAME,
    "diagnosis.cause.text": TEXT,
    "diagnosis.cause.action": TEXT,
    "diagnosis.leak": OBJECT,
    "diagnosis.leak.pid": COUNT,
    "diagnosis.leak.name": TEXT,
    "diagnosis.leak.from_mb": NUMBER,
    "diagnosis.leak.to_mb": NUMBER,
    "diagnosis.leak.minutes": NUMBER,
    "diagnosis.leak.mb_per_min": NUMBER,
    "diagnosis.leak.runs_out_min": NUMBER,
    "diagnosis.leak.text": TEXT,
    "diagnosis.top": LIST,
    "diagnosis.top[]": OBJECT,
    "diagnosis.top[].pid": COUNT,
    "diagnosis.top[].name": TEXT,
    "diagnosis.top[].rss_mb": NUMBER,
    "diagnosis.rates": OBJECT,
    "diagnosis.rates.*": NUMBER,
    "app": OBJECT,
    "app.name": TEXT,
    "app.psi": NUMBER,
    "app.psi_full": NUMBER,
    "app.current_mb": NUMBER,
    "app.limit_mb": NUMBER,
    "app.anon_mb": NUMBER,
    "app.file_mb": NUMBER,
    "app.oom_kills": COUNT,
    "app.cpu_vcpu": NUMBER,
    "app.verdict": OBJECT,
    "app.verdict.key": NAME,
    "app.verdict.text": TEXT,
    "app.verdict.action": TEXT,
}


def _check_for(path):
    if path in FIELDS:
        return FIELDS[path]
    parent, dot, _ = path.rpartition(".")
    return FIELDS.get(parent + ".*") if dot else None


def _walk(value, path, depth):
    """Every value in the sample with its path, outermost first."""
    yield path, value, depth
    if isinstance(value, dict):
        for key, item in value.items():
            yield from _walk(item, "%s.%s" % (path, key) if path else str(key), depth + 1)
    elif isinstance(value, list):
        for item in value:
            yield from _walk(item, path + "[]", depth + 1)


def problem(sample):
    """What is wrong with a sample, as (field, why), or None if it can be kept."""
    if not isinstance(sample, dict):
        return "", "a sample should be a JSON object"
    for path, value, depth in _walk(sample, "", 0):
        if depth > MAX_DEPTH:
            return path, "is nested too deeply"
        if isinstance(value, float) and not math.isfinite(value):
            return path, "should be a finite number"
        check = _check_for(path) if path else None
        if check is not None and value is not None:
            why = check(value)
            if why:
                return path, why
    return None
