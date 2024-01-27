"""Decide which resources in a snapshot are unused and estimate their cost."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone

from .pricing import HOURS_PER_MONTH

# Target states that mean "this target can receive traffic". "unavailable" is
# what targets report when health checks are disabled (the default for Lambda
# targets), so it is not evidence that nothing is being served.
_SERVING_STATES = {"healthy", "unavailable"}


@dataclass
class Finding:
    region: str
    check: str
    resource_id: str
    name: str
    reason: str
    monthly_cost: float
    cost_is_upper_bound: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _age_days(value: str | None, now: datetime) -> int | None:
    parsed = _parse_time(value)
    return None if parsed is None else (now - parsed).days


def unattached_volumes(region: str, data: dict, prices: dict) -> list[Finding]:
    out = []
    for v in data.get("volumes", []):
        if v["State"] != "available":
            continue
        per_gb = prices.get(f"ebs_gb_month.{v['VolumeType']}", prices["ebs_gb_month.gp2"])
        out.append(Finding(region, "ebs", v["VolumeId"], v.get("Name", ""),
                           f"unattached {v['VolumeType']} volume, {v['Size']} GiB",
                           round(v["Size"] * per_gb, 2)))
    return out


def old_snapshots(region: str, data: dict, prices: dict, now: datetime, min_age_days: int) -> list[Finding]:
    backing_amis = set(data.get("ami_snapshot_ids", []))
    out = []
    for s in data.get("snapshots", []):
        age = _age_days(s.get("StartTime"), now)
        if age is None or age < min_age_days or s["SnapshotId"] in backing_amis:
            continue
        tier = s.get("StorageTier") or "standard"
        per_gb = prices.get(f"snapshot_gb_month.{tier}", prices["snapshot_gb_month.standard"])
        out.append(Finding(region, "snapshots", s["SnapshotId"], s.get("Name", ""),
                           f"snapshot is {age} days old, not used by any AMI you own, "
                           f"source volume {s['VolumeSize']} GiB",
                           round(s["VolumeSize"] * per_gb, 2), cost_is_upper_bound=True))
    return out


def idle_addresses(region: str, data: dict, prices: dict) -> list[Finding]:
    monthly = round(prices["public_ipv4_hour"] * HOURS_PER_MONTH, 2)
    return [
        Finding(region, "eip", a.get("AllocationId") or a.get("PublicIp", ""), a.get("Name", ""),
                f"Elastic IP {a.get('PublicIp', '')} is not associated with anything", monthly)
        for a in data.get("addresses", [])
        if not (a.get("AssociationId") or a.get("InstanceId") or a.get("NetworkInterfaceId"))
    ]


def idle_load_balancers(region: str, data: dict, prices: dict) -> list[Finding]:
    out = []
    for lb in data.get("load_balancers", []):
        if lb.get("State") not in ("active", "active_impaired"):
            continue
        groups = lb.get("TargetGroups", [])
        states = [state for g in groups for state in g.get("TargetStates", [])]
        if not groups:
            reason = "no target groups attached"
        elif not states:
            reason = f"{len(groups)} target group(s), no registered targets"
        elif not any(state in _SERVING_STATES for state in states):
            reason = f"0 of {len(states)} registered target(s) healthy"
        else:
            continue
        hourly = prices.get(f"load_balancer_hour.{lb.get('Type')}", prices["load_balancer_hour.application"])
        out.append(Finding(region, "elb", lb["LoadBalancerName"], "",
                           f"{lb.get('Type', 'application')} load balancer: {reason}",
                           round(hourly * HOURS_PER_MONTH, 2)))
    return out


def unused_security_groups(region: str, data: dict) -> list[Finding]:
    attached = set(data.get("eni_group_ids", []))
    groups = data.get("security_groups", [])
    referenced_by: dict[str, list[str]] = {}
    for g in groups:
        for target in g.get("ReferencesGroupIds", []):
            referenced_by.setdefault(target, []).append(g["GroupId"])
    out = []
    for g in groups:
        # the default group of a VPC cannot be deleted, so it is never reported
        if g["GroupId"] in attached or g.get("GroupName") == "default":
            continue
        reason = "not attached to any network interface"
        if g["GroupId"] in referenced_by:
            reason += "; still referenced in rules of " + ", ".join(sorted(referenced_by[g["GroupId"]]))
        out.append(Finding(region, "sg", g["GroupId"], g.get("GroupName", ""), reason, 0.0))
    return out


def idle_nat_gateways(region: str, data: dict, prices: dict, now: datetime) -> list[Finding]:
    out = []
    for nat in data.get("nat_gateways", []):
        days = nat.get("TrafficDays", 0)
        age = _age_days(nat.get("CreateTime"), now)
        if nat.get("State") != "available" or nat.get("TrafficBytes") is None:
            continue
        if nat["TrafficBytes"] > 0 or age is None or age < days:
            continue  # has traffic, or too new to have a full window of metrics
        out.append(Finding(region, "nat", nat["NatGatewayId"], nat.get("Name", ""),
                           f"NAT gateway carried 0 bytes in the last {days} days",
                           round(prices["nat_gateway_hour"] * HOURS_PER_MONTH, 2)))
    return out


def analyze(snapshot: dict, prices: dict, snapshot_age_days: int = 90) -> list[Finding]:
    now = _parse_time(snapshot.get("collected_at")) or datetime.now(timezone.utc)
    findings: list[Finding] = []
    for region, data in snapshot.get("regions", {}).items():
        findings += unattached_volumes(region, data, prices)
        findings += old_snapshots(region, data, prices, now, snapshot_age_days)
        findings += idle_addresses(region, data, prices)
        findings += idle_load_balancers(region, data, prices)
        findings += unused_security_groups(region, data)
        findings += idle_nat_gateways(region, data, prices, now)
    findings.sort(key=lambda f: (f.region, -f.monthly_cost, f.check, f.resource_id))
    return findings
