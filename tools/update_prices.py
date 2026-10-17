#!/usr/bin/env python3
"""Refresh data/prices.json from AWS's public on-demand price list.

    python3 tools/update_prices.py                  # the default regions
    python3 tools/update_prices.py us-east-1 me-south-1

The right-sizing report says its answer in money ("t3.medium, $30 a month"),
so it needs a price for every size it compares. These come from the same price
files the AWS pricing pages load: public, no AWS account or credentials needed.

Prices change rarely, so they are kept in the repo rather than fetched at run
time: the dashboard server often runs where it cannot reach the internet. The
file records when it was fetched, and the report shows that date beside every
price. Run this again to bring it up to date.

Only Linux on-demand prices, and only the families likely to be right-sized for
one app (burstable, general purpose, compute and memory optimised). Anything
else can be priced by hand: see SYSHEALTH_PRICES_FILE in pricing.py.
"""

import gzip
import json
import os
import sys
import time
import urllib.parse
import urllib.request

LOCATIONS_URL = "https://b0.p.awsstatic.com/locations/1.0/aws/current/locations.json"
PRICES_URL = ("https://b0.p.awsstatic.com/pricing/2.0/meteredUnitMaps/ec2/USD/current/"
              "ec2-ondemand-without-sec-sel/%s/Linux/index.json")

DEFAULT_REGIONS = [
    "us-east-1", "us-east-2", "us-west-2",
    "eu-west-1", "eu-central-1", "eu-north-1",
    "ap-south-1", "ap-southeast-1", "ap-northeast-1",
]

FAMILIES = {
    "t2", "t3", "t3a", "t4g",
    "m5", "m6i", "m6a", "m6g", "m7i", "m7g",
    "c5", "c6i", "c6g", "c7i", "c7g",
    "r5", "r6i", "r6g", "r7i", "r7g",
}

# Sizes above this are rarely what one app's memory is weighed against, and
# leaving them out keeps the file small enough to read.
MAX_SIZES = {"nano", "micro", "small", "medium", "large", "xlarge", "2xlarge", "4xlarge"}

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "prices.json")


def fetch_json(url):
    request = urllib.request.Request(url, headers={"Accept-Encoding": "gzip"})
    with urllib.request.urlopen(request, timeout=60) as response:
        body = response.read()
        if response.headers.get("Content-Encoding") == "gzip" or body[:2] == b"\x1f\x8b":
            body = gzip.decompress(body)
    return json.loads(body)


def region_names():
    """Region code -> the location name the price files are keyed by."""
    names = {}
    for name, entry in fetch_json(LOCATIONS_URL).items():
        if isinstance(entry, dict) and entry.get("type") == "AWS Region" and entry.get("code"):
            names[entry["code"]] = entry.get("name") or name
    return names


def gib(text):
    """'4 GiB' -> 4.0; '0.5 GiB' -> 0.5."""
    try:
        return float(str(text).split()[0].replace(",", ""))
    except (ValueError, IndexError):
        return None


def region_prices(location):
    data = fetch_json(PRICES_URL % urllib.parse.quote(location))
    out = {}
    for rows in data.get("regions", {}).values():
        for row in rows.values():
            itype = row.get("Instance Type") or ""
            family, _, size = itype.partition(".")
            if family not in FAMILIES or size not in MAX_SIZES:
                continue
            try:
                price = float(row["price"])
            except (KeyError, ValueError):
                continue
            if price <= 0:
                continue
            out[itype] = {
                "usd_hour": round(price, 6),
                "memory_gib": gib(row.get("Memory")),
                "vcpu": int(row["vCPU"]) if str(row.get("vCPU", "")).isdigit() else None,
            }
    return dict(sorted(out.items()))


def main(argv):
    regions = argv or DEFAULT_REGIONS
    names = region_names()
    table = {}
    for code in regions:
        if code not in names:
            sys.exit("Unknown region %r. Known: %s" % (code, ", ".join(sorted(names))))
        prices = region_prices(names[code])
        if not prices:
            sys.exit("No prices came back for %s (%s)." % (code, names[code]))
        table[code] = prices
        print("%-15s %3d instance types" % (code, len(prices)))

    # One instance type per line, so a price change reads as a one-line diff.
    lines = [
        "{",
        '"source": %s,' % json.dumps("AWS public price list, Linux on-demand, USD"),
        '"fetched": %s,' % json.dumps(time.strftime("%Y-%m-%d", time.gmtime())),
        '"regions": {',
    ]
    for r, code in enumerate(regions):
        lines.append(" %s: {" % json.dumps(code))
        types = list(table[code].items())
        for i, (itype, row) in enumerate(types):
            lines.append("  %s: %s%s" % (json.dumps(itype), json.dumps(row),
                                         "," if i < len(types) - 1 else ""))
        lines.append(" }%s" % ("," if r < len(regions) - 1 else ""))
    lines += ["}", "}"]

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w") as f:
        f.write("\n".join(lines) + "\n")
    json.load(open(OUT))  # what was written must still parse
    print("Wrote %s" % os.path.normpath(OUT))


if __name__ == "__main__":
    main(sys.argv[1:])
