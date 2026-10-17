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
"""

import json
import os

HOURS_PER_MONTH = 730  # AWS's own figure for a month, in its calculator and bills

DATA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "prices.json")
DEFAULT_REGION = os.environ.get("SYSHEALTH_PRICE_REGION") or "us-east-1"

_table = None


def _load():
    global _table
    if _table is not None:
        return _table
    table = {"fetched": None, "regions": {}, "overrides": {}, "flat": {}}
    try:
        with open(DATA_FILE) as f:
            doc = json.load(f)
        table["fetched"] = doc.get("fetched")
        for region, rows in (doc.get("regions") or {}).items():
            table["regions"][region] = {
                itype: float(row["usd_hour"]) for itype, row in rows.items()
                if isinstance(row, dict) and row.get("usd_hour")
            }
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
