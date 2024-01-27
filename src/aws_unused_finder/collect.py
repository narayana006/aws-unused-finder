"""Collect the facts needed to decide what is unused. Describe/Get calls only.

`collect` returns a plain JSON-serialisable snapshot. Analysis works on the
snapshot, so a saved snapshot can be re-analysed offline with `--from-file`.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from botocore.exceptions import BotoCoreError, ClientError

from .guard import guarded_client

SCHEMA_VERSION = 1
ALL_CHECKS = ("ebs", "snapshots", "eip", "elb", "sg", "nat")
_NAT_TRAFFIC_METRICS = ("BytesOutToDestination", "BytesInFromDestination",
                        "BytesOutToSource", "BytesInFromSource")


def _iso(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()
    return str(value)


def _name(resource: dict) -> str:
    for tag in resource.get("Tags") or []:
        if tag.get("Key") == "Name":
            return tag.get("Value", "")
    return ""


def _pages(client, operation: str, key: str, **kwargs) -> list[dict]:
    items: list[dict] = []
    for page in client.get_paginator(operation).paginate(**kwargs):
        items.extend(page.get(key, []))
    return items


def collect_volumes(ec2) -> list[dict]:
    return [{
        "VolumeId": v["VolumeId"], "Name": _name(v), "Size": v["Size"],
        "VolumeType": v.get("VolumeType", "gp2"), "State": v["State"],
        "CreateTime": _iso(v.get("CreateTime")), "AvailabilityZone": v.get("AvailabilityZone", ""),
    } for v in _pages(ec2, "describe_volumes", "Volumes")]


def collect_snapshots(ec2) -> dict:
    snapshots = [{
        "SnapshotId": s["SnapshotId"], "Name": _name(s), "VolumeSize": s["VolumeSize"],
        "StartTime": _iso(s.get("StartTime")), "Description": s.get("Description", ""),
        "StorageTier": s.get("StorageTier", "standard"), "State": s.get("State", "completed"),
    } for s in _pages(ec2, "describe_snapshots", "Snapshots", OwnerIds=["self"])]
    in_use = sorted({
        mapping["Ebs"]["SnapshotId"]
        for image in _pages(ec2, "describe_images", "Images", Owners=["self"])
        for mapping in image.get("BlockDeviceMappings", [])
        if mapping.get("Ebs", {}).get("SnapshotId")
    })
    return {"snapshots": snapshots, "ami_snapshot_ids": in_use}


def collect_addresses(ec2) -> list[dict]:
    return [{
        "AllocationId": a.get("AllocationId", ""), "PublicIp": a.get("PublicIp", ""), "Name": _name(a),
        "AssociationId": a.get("AssociationId") or "", "InstanceId": a.get("InstanceId") or "",
        "NetworkInterfaceId": a.get("NetworkInterfaceId") or "",
    } for a in ec2.describe_addresses().get("Addresses", [])]


def _target_groups(elbv2, load_balancer_arn: str) -> list[dict]:
    try:
        return _pages(elbv2, "describe_target_groups", "TargetGroups", LoadBalancerArn=load_balancer_arn)
    except ClientError as exc:
        # some endpoints answer "not found" instead of an empty list
        if exc.response.get("Error", {}).get("Code") == "TargetGroupNotFound":
            return []
        raise


def collect_load_balancers(elbv2) -> list[dict]:
    out = []
    for lb in _pages(elbv2, "describe_load_balancers", "LoadBalancers"):
        groups = []
        for tg in _target_groups(elbv2, lb["LoadBalancerArn"]):
            health = elbv2.describe_target_health(TargetGroupArn=tg["TargetGroupArn"])
            groups.append({
                "TargetGroupName": tg.get("TargetGroupName", ""),
                "TargetStates": [d["TargetHealth"]["State"]
                                 for d in health.get("TargetHealthDescriptions", [])],
            })
        out.append({
            "LoadBalancerArn": lb["LoadBalancerArn"], "LoadBalancerName": lb["LoadBalancerName"],
            "Type": lb.get("Type", "application"), "State": lb.get("State", {}).get("Code", ""),
            "CreatedTime": _iso(lb.get("CreatedTime")), "TargetGroups": groups,
        })
    return out


def collect_security_groups(ec2) -> dict:
    groups = []
    for g in _pages(ec2, "describe_security_groups", "SecurityGroups"):
        references = {
            pair["GroupId"]
            for rule in g.get("IpPermissions", []) + g.get("IpPermissionsEgress", [])
            for pair in rule.get("UserIdGroupPairs", [])
            if pair.get("GroupId") and pair["GroupId"] != g["GroupId"]
        }
        groups.append({
            "GroupId": g["GroupId"], "GroupName": g.get("GroupName", ""), "VpcId": g.get("VpcId", ""),
            "Description": g.get("Description", ""), "ReferencesGroupIds": sorted(references),
        })
    attached = sorted({
        group["GroupId"]
        for eni in _pages(ec2, "describe_network_interfaces", "NetworkInterfaces")
        for group in eni.get("Groups", [])
    })
    return {"security_groups": groups, "eni_group_ids": attached}


def nat_traffic_bytes(cloudwatch, nat_gateway_id: str, start: datetime, end: datetime) -> float:
    total = 0.0
    for metric in _NAT_TRAFFIC_METRICS:
        stats = cloudwatch.get_metric_statistics(
            Namespace="AWS/NATGateway", MetricName=metric,
            Dimensions=[{"Name": "NatGatewayId", "Value": nat_gateway_id}],
            StartTime=start, EndTime=end, Period=86400, Statistics=["Sum"],
        )
        total += sum(point.get("Sum", 0.0) for point in stats.get("Datapoints", []))
    return total


def collect_nat_gateways(ec2, cloudwatch, now: datetime, idle_days: int) -> list[dict]:
    out = []
    start = now - timedelta(days=idle_days)
    for nat in _pages(ec2, "describe_nat_gateways", "NatGateways"):
        record = {
            "NatGatewayId": nat["NatGatewayId"], "Name": _name(nat), "State": nat.get("State", ""),
            "VpcId": nat.get("VpcId", ""), "SubnetId": nat.get("SubnetId", ""),
            "CreateTime": _iso(nat.get("CreateTime")), "TrafficBytes": None, "TrafficDays": idle_days,
        }
        if record["State"] == "available":
            record["TrafficBytes"] = nat_traffic_bytes(cloudwatch, nat["NatGatewayId"], start, now)
        out.append(record)
    return out


def collect_region(session, region: str, checks=ALL_CHECKS, idle_days: int = 14,
                   now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    ec2 = guarded_client(session, "ec2", region)
    data: dict = {"errors": []}

    def attempt(check: str, work) -> None:
        if check not in checks:
            return
        try:
            result = work()
        except (ClientError, BotoCoreError) as exc:
            data["errors"].append({"check": check, "error": str(exc)})
            return
        data.update(result)

    attempt("ebs", lambda: {"volumes": collect_volumes(ec2)})
    attempt("snapshots", lambda: collect_snapshots(ec2))
    attempt("eip", lambda: {"addresses": collect_addresses(ec2)})
    attempt("elb", lambda: {"load_balancers": collect_load_balancers(guarded_client(session, "elbv2", region))})
    attempt("sg", lambda: collect_security_groups(ec2))
    attempt("nat", lambda: {"nat_gateways": collect_nat_gateways(
        ec2, guarded_client(session, "cloudwatch", region), now, idle_days)})
    return data


def collect(session, regions: list[str], checks=ALL_CHECKS, idle_days: int = 14,
            now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    return {
        "schema": SCHEMA_VERSION,
        "collected_at": _iso(now),
        "regions": {region: collect_region(session, region, checks, idle_days, now) for region in regions},
    }
