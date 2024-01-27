"""Default prices used for the monthly cost estimate.

The defaults are the public on-demand list prices for US East (N. Virginia),
taken from the AWS pricing pages (EBS, VPC public IPv4 and NAT gateway,
Elastic Load Balancing). Prices differ by region and change over time, and the
same table is applied to every region, so treat the result as an estimate.
Override any value with `--prices FILE`.
"""

from __future__ import annotations

import json

HOURS_PER_MONTH = 730

DEFAULT_PRICES: dict[str, float] = {
    # EBS volume storage, USD per GB-month (provisioned IOPS/throughput not included)
    "ebs_gb_month.gp3": 0.08,
    "ebs_gb_month.gp2": 0.10,
    "ebs_gb_month.io1": 0.125,
    "ebs_gb_month.io2": 0.125,
    "ebs_gb_month.st1": 0.045,
    "ebs_gb_month.sc1": 0.015,
    "ebs_gb_month.standard": 0.05,
    # EBS snapshot storage, USD per GB-month
    "snapshot_gb_month.standard": 0.05,
    "snapshot_gb_month.archive": 0.0125,
    # USD per hour
    "public_ipv4_hour": 0.005,
    "nat_gateway_hour": 0.045,
    "load_balancer_hour.application": 0.0225,
    "load_balancer_hour.network": 0.0225,
    "load_balancer_hour.gateway": 0.0125,
}


def load_prices(path: str | None = None) -> dict[str, float]:
    prices = dict(DEFAULT_PRICES)
    if not path:
        return prices
    with open(path, encoding="utf-8") as handle:
        overrides = json.load(handle)
    if not isinstance(overrides, dict):
        raise ValueError(f"{path}: expected a JSON object of price overrides")
    for key, value in overrides.items():
        if key not in DEFAULT_PRICES:
            raise ValueError(f"{path}: unknown price key {key!r}; valid keys: {', '.join(sorted(DEFAULT_PRICES))}")
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            raise ValueError(f"{path}: price for {key!r} must be a non-negative number")
        prices[key] = float(value)
    return prices
