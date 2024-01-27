from datetime import datetime, timedelta, timezone

import boto3
import pytest
from botocore.stub import Stubber

from aws_unused_finder.analyze import analyze
from aws_unused_finder.collect import collect, collect_region
from aws_unused_finder.guard import ALLOWED_OPERATIONS, ReadOnlyViolation, guarded_client, required_iam_actions
from aws_unused_finder.pricing import DEFAULT_PRICES

REGION = "us-east-1"


def record_calls(session):
    """Record every API operation made through clients created from this session."""
    calls = []
    session.events.register("before-call", lambda model, **_: calls.append(model.name))
    return calls


def network(ec2):
    vpc = ec2.create_vpc(CidrBlock="10.0.0.0/16")["Vpc"]["VpcId"]
    a = ec2.create_subnet(VpcId=vpc, CidrBlock="10.0.1.0/24", AvailabilityZone="us-east-1a")["Subnet"]["SubnetId"]
    b = ec2.create_subnet(VpcId=vpc, CidrBlock="10.0.2.0/24", AvailabilityZone="us-east-1b")["Subnet"]["SubnetId"]
    return vpc, a, b


def by_check(findings):
    out = {}
    for f in findings:
        out.setdefault(f.check, []).append(f)
    return out


def test_volumes_addresses_and_security_groups(aws):
    ec2 = aws.client("ec2")
    vpc, subnet, _ = network(ec2)
    used_sg = ec2.create_security_group(GroupName="used", Description="d", VpcId=vpc)["GroupId"]
    spare_sg = ec2.create_security_group(GroupName="spare", Description="d", VpcId=vpc)["GroupId"]
    image = ec2.register_image(Name="base", Architecture="x86_64", RootDeviceName="/dev/sda1")["ImageId"]
    instance = ec2.run_instances(ImageId=image, MinCount=1, MaxCount=1, SubnetId=subnet,
                                 SecurityGroupIds=[used_sg])["Instances"][0]["InstanceId"]
    attached = ec2.create_volume(Size=10, AvailabilityZone="us-east-1a", VolumeType="gp3")["VolumeId"]
    ec2.attach_volume(VolumeId=attached, InstanceId=instance, Device="/dev/sdf")
    orphan = ec2.create_volume(Size=40, AvailabilityZone="us-east-1a", VolumeType="gp2", TagSpecifications=[
        {"ResourceType": "volume", "Tags": [{"Key": "Name", "Value": "left-behind"}]}])["VolumeId"]
    idle_ip = ec2.allocate_address(Domain="vpc")["AllocationId"]
    used_ip = ec2.allocate_address(Domain="vpc")["AllocationId"]
    ec2.associate_address(AllocationId=used_ip, InstanceId=instance)

    found = by_check(analyze(collect(aws, [REGION]), DEFAULT_PRICES))

    assert [(f.resource_id, f.name, f.monthly_cost) for f in found["ebs"]] == [(orphan, "left-behind", 4.0)]
    assert [f.resource_id for f in found["eip"]] == [idle_ip]
    assert [f.resource_id for f in found["sg"]] == [spare_sg]


def test_snapshots_backing_an_ami_are_not_reported(aws):
    ec2 = aws.client("ec2")
    volume = ec2.create_volume(Size=30, AvailabilityZone="us-east-1a")["VolumeId"]
    plain = ec2.create_snapshot(VolumeId=volume, Description="manual")["SnapshotId"]
    image = ec2.register_image(Name="base", Architecture="x86_64", RootDeviceName="/dev/sda1")["ImageId"]
    backing = ec2.describe_images(ImageIds=[image])["Images"][0]["BlockDeviceMappings"][0]["Ebs"]["SnapshotId"]

    snapshot = collect(aws, [REGION], checks=["snapshots"])
    data = snapshot["regions"][REGION]
    assert {s["SnapshotId"] for s in data["snapshots"]} == {plain, backing}
    assert data["ami_snapshot_ids"] == [backing]

    # moto stamps snapshots with the current time, so use a zero-day threshold
    found = analyze(snapshot, DEFAULT_PRICES, snapshot_age_days=0)
    assert [(f.resource_id, f.monthly_cost, f.cost_is_upper_bound) for f in found] == [(plain, 1.5, True)]
    assert analyze(snapshot, DEFAULT_PRICES, snapshot_age_days=90) == []


def test_load_balancer_target_health(aws):
    ec2, elbv2 = aws.client("ec2"), aws.client("elbv2")
    vpc, a, b = network(ec2)
    image = ec2.register_image(Name="base", Architecture="x86_64", RootDeviceName="/dev/sda1")["ImageId"]
    running, stopped = [i["InstanceId"] for i in ec2.run_instances(
        ImageId=image, MinCount=2, MaxCount=2, SubnetId=a)["Instances"]]

    def make(name, targets):
        arn = elbv2.create_load_balancer(Name=name, Subnets=[a, b])["LoadBalancers"][0]["LoadBalancerArn"]
        if targets is None:
            return
        tg = elbv2.create_target_group(Name=f"{name}-tg", Protocol="HTTP", Port=80,
                                       VpcId=vpc)["TargetGroups"][0]["TargetGroupArn"]
        elbv2.create_listener(LoadBalancerArn=arn, Protocol="HTTP", Port=80,
                              DefaultActions=[{"Type": "forward", "TargetGroupArn": tg}])
        if targets:
            elbv2.register_targets(TargetGroupArn=tg, Targets=[{"Id": t} for t in targets])

    make("healthy", [running])
    make("stopped-target", [stopped])
    make("empty-group", [])
    make("no-group", None)
    ec2.stop_instances(InstanceIds=[stopped])

    snapshot = collect(aws, [REGION], checks=["elb"])
    assert snapshot["regions"][REGION]["errors"] == []
    found = {f.resource_id: f.reason for f in analyze(snapshot, DEFAULT_PRICES)}
    assert set(found) == {"stopped-target", "empty-group", "no-group"}
    assert "0 of 1 registered target(s) healthy" in found["stopped-target"]
    assert "no registered targets" in found["empty-group"]
    assert "no target groups" in found["no-group"]


def test_nat_gateway_traffic_from_cloudwatch(aws):
    ec2, cloudwatch = aws.client("ec2"), aws.client("cloudwatch")
    _, a, b = network(ec2)
    busy = ec2.create_nat_gateway(SubnetId=a, AllocationId=ec2.allocate_address(Domain="vpc")["AllocationId"])
    idle = ec2.create_nat_gateway(SubnetId=b, AllocationId=ec2.allocate_address(Domain="vpc")["AllocationId"])
    busy_id, idle_id = busy["NatGateway"]["NatGatewayId"], idle["NatGateway"]["NatGatewayId"]
    now = datetime.now(timezone.utc)
    cloudwatch.put_metric_data(Namespace="AWS/NATGateway", MetricData=[{
        "MetricName": "BytesInFromSource", "Value": 5000.0, "Timestamp": now - timedelta(hours=2),
        "Dimensions": [{"Name": "NatGatewayId", "Value": busy_id}]}])

    snapshot = collect(aws, [REGION], checks=["nat"], idle_days=14)
    traffic = {n["NatGatewayId"]: n["TrafficBytes"] for n in snapshot["regions"][REGION]["nat_gateways"]}
    assert traffic == {busy_id: 5000.0, idle_id: 0.0}

    # Both gateways were created seconds ago, so neither has a full 14-day window yet.
    assert analyze(snapshot, DEFAULT_PRICES) == []
    # Same data seen 30 days later: the silent one is now reported.
    snapshot["collected_at"] = (now + timedelta(days=30)).isoformat()
    found = analyze(snapshot, DEFAULT_PRICES)
    assert [(f.resource_id, f.monthly_cost) for f in found] == [(idle_id, 32.85)]
    # NAT gateway EIPs are associated, so they are not idle addresses
    assert by_check(analyze(collect(aws, [REGION]), DEFAULT_PRICES)).get("eip") is None


def test_full_scan_only_makes_allow_listed_read_calls(aws):
    ec2, elbv2 = aws.client("ec2"), aws.client("elbv2")
    vpc, a, b = network(ec2)
    ec2.create_nat_gateway(SubnetId=a, AllocationId=ec2.allocate_address(Domain="vpc")["AllocationId"])
    arn = elbv2.create_load_balancer(Name="lb", Subnets=[a, b])["LoadBalancers"][0]["LoadBalancerArn"]
    tg = elbv2.create_target_group(Name="tg", Protocol="HTTP", Port=80, VpcId=vpc)["TargetGroups"][0]["TargetGroupArn"]
    elbv2.create_listener(LoadBalancerArn=arn, Protocol="HTTP", Port=80,
                          DefaultActions=[{"Type": "forward", "TargetGroupArn": tg}])
    before = {
        "volumes": ec2.describe_volumes()["Volumes"], "addresses": ec2.describe_addresses()["Addresses"],
        "nat": [n["State"] for n in ec2.describe_nat_gateways()["NatGateways"]],
        "lbs": [l["LoadBalancerArn"] for l in elbv2.describe_load_balancers()["LoadBalancers"]],
        "groups": [g["GroupId"] for g in ec2.describe_security_groups()["SecurityGroups"]],
    }

    scan_session = boto3.Session(region_name=REGION)
    calls = record_calls(scan_session)
    collect(scan_session, [REGION, "eu-west-1"])

    allowed = set().union(*ALLOWED_OPERATIONS.values())
    assert calls and set(calls) <= allowed
    assert all(name.startswith(("Describe", "Get")) for name in calls)
    assert set(calls) == allowed  # every allow-listed call is actually needed
    after = {
        "volumes": ec2.describe_volumes()["Volumes"], "addresses": ec2.describe_addresses()["Addresses"],
        "nat": [n["State"] for n in ec2.describe_nat_gateways()["NatGateways"]],
        "lbs": [l["LoadBalancerArn"] for l in elbv2.describe_load_balancers()["LoadBalancers"]],
        "groups": [g["GroupId"] for g in ec2.describe_security_groups()["SecurityGroups"]],
    }
    assert after == before


def test_guard_refuses_mutating_calls_before_they_are_sent(aws):
    ec2 = aws.client("ec2")
    volume = ec2.create_volume(Size=1, AvailabilityZone="us-east-1a")["VolumeId"]
    guarded = guarded_client(aws, "ec2", REGION)
    for call in (lambda: guarded.delete_volume(VolumeId=volume),
                 lambda: guarded.release_address(AllocationId="eipalloc-00000000"),
                 lambda: guarded.describe_instances()):
        with pytest.raises(ReadOnlyViolation):
            call()
    assert [v["VolumeId"] for v in ec2.describe_volumes()["Volumes"]] == [volume]
    with pytest.raises(ReadOnlyViolation):
        guarded_client(aws, "s3", REGION)


def test_required_iam_actions_match_the_allow_list():
    actions = required_iam_actions()
    assert len(actions) == 11
    assert "elasticloadbalancing:DescribeTargetHealth" in actions
    assert all(a.split(":")[1].startswith(("Describe", "Get")) for a in actions)


class StubbedSession:
    """Hands out pre-built clients so botocore's Stubber can script the responses."""

    def __init__(self, **clients):
        self.clients = clients

    def client(self, service, region_name=None):
        return self.clients[service]


def test_access_denied_on_one_check_is_recorded_and_others_continue():
    ec2 = boto3.client("ec2", region_name=REGION)
    with Stubber(ec2) as stub:
        stub.add_client_error("describe_volumes", "UnauthorizedOperation", "You are not authorized")
        stub.add_response("describe_addresses", {"Addresses": [
            {"AllocationId": "eipalloc-0abc", "PublicIp": "203.0.113.7", "Domain": "vpc"}]})
        data = collect_region(StubbedSession(ec2=ec2), REGION, checks=["ebs", "eip"])
        stub.assert_no_pending_responses()

    assert [e["check"] for e in data["errors"]] == ["ebs"]
    assert "UnauthorizedOperation" in data["errors"][0]["error"]
    assert "volumes" not in data
    assert data["addresses"][0]["AllocationId"] == "eipalloc-0abc"


def test_paginated_volumes_are_all_collected():
    ec2 = boto3.client("ec2", region_name=REGION)
    page = lambda vid: {"VolumeId": vid, "Size": 8, "VolumeType": "gp3", "State": "available",
                        "CreateTime": datetime(2026, 1, 1, tzinfo=timezone.utc), "AvailabilityZone": "us-east-1a"}
    with Stubber(ec2) as stub:
        stub.add_response("describe_volumes", {"Volumes": [page("vol-00000001")], "NextToken": "t1"})
        stub.add_response("describe_volumes", {"Volumes": [page("vol-00000002")]}, {"NextToken": "t1"})
        data = collect_region(StubbedSession(ec2=ec2), REGION, checks=["ebs"])
    assert [v["VolumeId"] for v in data["volumes"]] == ["vol-00000001", "vol-00000002"]
    assert data["volumes"][0]["CreateTime"] == "2026-01-01T00:00:00+00:00"
