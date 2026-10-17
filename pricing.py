"""What an instance size costs, so a recommendation can be said in money.

Prices come from data/prices.json, a snapshot of AWS's public Linux on-demand
price list made by tools/update_prices.py. The snapshot carries its date, and
the report shows it, because a price without a date is a guess.

The region is the one each agent reports (from the EC2 metadata service). Off
EC2, or for an agent too old to say, SYSHEALTH_PRICE_REGION picks the region,
and us-east-1 is the last resort.

Prices for anything the snapshot lacks (a family it does not cover, a
discounted rate, a cloud that is not AWS) go in a JSON file named by
SYSHEALTH_PRICES_FILE, either flat or by region:

    {"t3.micro": 0.0104, "t3.small": 0.0208}
    {"ap-south-1": {"t3.micro": 0.0112}}

Those always win over the snapshot.

Burstable sizes (t2, t3, t3a, t4g, t8i) also have a CPU price: each earns CPU
credits at a set rate, and an app that uses more CPU on average than those
credits cover either pays for the rest (unlimited mode, the default for all
but t2) or is held down to that rate (standard mode, the t2 default). See
burst() below; the figures are AWS's own, sources inline.
"""

import json
import os

HOURS_PER_MONTH = 730  # AWS's own figure for a month, in its calculator and bills

DATA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "prices.json")
DEFAULT_REGION = os.environ.get("SYSHEALTH_PRICE_REGION") or "us-east-1"

# CPU credits each burstable size earns an hour, from AWS's credit table
# (EC2 User Guide, "Key concepts for burstable performance instances"). One
# credit is one vCPU busy for a minute, so credits / 60 is the baseline: the
# vCPUs the size can keep busy on average at no extra cost.
CREDITS_PER_HOUR = {
    "t2.nano": 3, "t2.micro": 6, "t2.small": 12, "t2.medium": 24, "t2.large": 36,
    "t2.xlarge": 54, "t2.2xlarge": 81.6,
    "t8i.nano": 6, "t8i.micro": 12, "t8i.small": 24, "t8i.medium": 24,
}
for _family in ("t3", "t3a", "t4g"):
    CREDITS_PER_HOUR.update({
        _family + ".nano": 6, _family + ".micro": 12, _family + ".small": 24,
        _family + ".medium": 24, _family + ".large": 36, _family + ".xlarge": 96,
        _family + ".2xlarge": 192,
    })

# What a vCPU-hour above the baseline costs in unlimited mode, Linux. AWS's
# on-demand pricing page ("T2/T3/T4g Unlimited Mode Pricing") gives one rate
# for every region and size. It names T2, T3, T4g and T8i; T3a is not listed
# on its own and is charged here at the T3 rate.
SURPLUS_USD_PER_VCPU_HOUR = {"t2": 0.05, "t3": 0.05, "t3a": 0.05, "t4g": 0.04, "t8i": 0.06}

# T3, T3a, T4g and T8i launch in unlimited mode unless told otherwise; T2
# launches in standard mode, where the CPU is held to the baseline once the
# credits it saved up run out.
STANDARD_BY_DEFAULT = ("t2",)

_table = None


def _load():
    global _table
    if _table is not None:
        return _table
    table = {"fetched": None, "regions": {}, "overrides": {}, "flat": {}, "specs": {}}
    try:
        with open(DATA_FILE) as f:
            doc = json.load(f)
        table["fetched"] = doc.get("fetched")
        for region, rows in (doc.get("regions") or {}).items():
            table["regions"][region] = {
                itype: float(row["usd_hour"]) for itype, row in rows.items()
                if isinstance(row, dict) and row.get("usd_hour")
            }
            # A size has the same vCPUs and memory in every region.
            for itype, row in rows.items():
                if isinstance(row, dict) and row.get("vcpu"):
                    table["specs"].setdefault(itype, {
                        "vcpu": int(row["vcpu"]), "memory_gib": row.get("memory_gib")})
    except (OSError, ValueError, KeyError, TypeError):
        pass

    path = os.environ.get("SYSHEALTH_PRICES_FILE")
    if path:
        try:
            with open(path) as f:
                doc = json.load(f)
            for key, value in doc.items():
                if isinstance(value, dict):
                    table["overrides"][key] = {k: float(v) for k, v in value.items()}
                else:
                    table["flat"][key] = float(value)
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            print("pricing: ignoring %s: %s" % (path, exc))
    _table = table
    return table


def lookup(instance_type, region=None):
    """{usd_hour, usd_month, region, source} for one size, or None if unknown."""
    if not instance_type or instance_type == "unknown":
        return None
    table = _load()
    region = region or DEFAULT_REGION

    for source, rows in (
        ("your prices file", table["overrides"].get(region, {})),
        ("your prices file", table["flat"]),
        ("AWS list price, %s" % (table["fetched"] or "undated"), table["regions"].get(region, {})),
    ):
        hourly = rows.get(instance_type)
        if hourly:
            return {
                "usd_hour": hourly,
                "usd_month": round(hourly * HOURS_PER_MONTH, 2),
                "region": region,
                "source": source,
            }
    return None


def fetched():
    return _load()["fetched"]


def specs(instance_type):
    """{vcpu, memory_gib} for a size the snapshot knows, else None."""
    return _load()["specs"].get(instance_type or "")


def burst(instance_type):
    """How a burstable size's CPU credits work, or None if it is not one.

    {baseline_vcpu, credits_per_hour, mode, usd_per_vcpu_hour}: the vCPUs it
    can keep busy on average for free, its default credit mode, and what each
    vCPU-hour above that costs in unlimited mode.
    """
    credits = CREDITS_PER_HOUR.get(instance_type or "")
    if credits is None:
        return None
    family = instance_type.split(".")[0]
    return {
        "baseline_vcpu": round(credits / 60.0, 3),
        "credits_per_hour": credits,
        "mode": "standard" if family in STANDARD_BY_DEFAULT else "unlimited",
        "usd_per_vcpu_hour": SURPLUS_USD_PER_VCPU_HOUR[family],
    }
