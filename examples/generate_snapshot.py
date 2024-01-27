"""Build examples/snapshot.json without touching a real AWS account.

It creates a small fake estate inside moto's in-memory AWS mock, runs the real
collector against it and saves the result. Requires the dev extras:

    pip install -e ".[dev]"
    python examples/generate_snapshot.py
"""

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Fake credentials, set before boto3 is imported: nothing here can reach AWS.
os.environ.update({
    "AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing",
    "AWS_SESSION_TOKEN": "testing", "AWS_DEFAULT_REGION": "us-east-1",
    "AWS_CONFIG_FILE": os.devnull, "AWS_SHARED_CREDENTIALS_FILE": os.devnull,
    "AWS_EC2_METADATA_DISABLED": "true", "MOTO_EC2_LOAD_DEFAULT_AMIS": "false",
})
os.environ.pop("AWS_PROFILE", None)

import boto3  # noqa: E402
from moto import mock_aws  # noqa: E402
from moto.core import DEFAULT_ACCOUNT_ID  # noqa: E402
from moto.ec2 import ec2_backends  # noqa: E402

from aws_unused_finder.cli import main  # noqa: E402

NOW = datetime.now(timezone.utc)


def name(resource_type: str, value: str) -> list[dict]:
    return [{"ResourceType": resource_type, "Tags": [{"Key": "Name", "Value": value}]}]


def seed_us_east_1(session) -> None:
    region = "us-east-1"
    ec2 = session.client("ec2", region_name=region)
    elbv2 = session.client("elbv2", region_name=region)
    cloudwatch = session.client("cloudwatch", region_name=region)
    backend = ec2_backends[DEFAULT_ACCOUNT_ID][region]

    vpc = ec2.create_vpc(CidrBlock="10.0.0.0/16")["Vpc"]["VpcId"]
    subnet_a = ec2.create_subnet(VpcId=vpc, CidrBlock="10.0.1.0/24", AvailabilityZone="us-east-1a")["Subnet"]["SubnetId"]
    subnet_b = ec2.create_subnet(VpcId=vpc, CidrBlock="10.0.2.0/24", AvailabilityZone="us-east-1b")["Subnet"]["SubnetId"]

    # Security groups: one in use by the instances, two not attached to anything.
    app_sg = ec2.create_security_group(GroupName="app-servers", Description="app tier", VpcId=vpc)["GroupId"]
    legacy = ec2.create_security_group(GroupName="legacy-app", Description="old app tier", VpcId=vpc)["GroupId"]
    legacy_db = ec2.create_security_group(GroupName="legacy-db", Description="old database", VpcId=vpc)["GroupId"]
    ec2.authorize_security_group_ingress(GroupId=legacy_db, IpPermissions=[{
        "IpProtocol": "tcp", "FromPort": 5432, "ToPort": 5432, "UserIdGroupPairs": [{"GroupId": legacy}]}])

    # Instances. moto creates one snapshot per registered AMI; it backs the AMI,
    # so it must not be reported however old it is.
    image = ec2.register_image(Name="golden-2026", Architecture="x86_64", RootDeviceName="/dev/sda1")["ImageId"]
    ami_snapshot = ec2.describe_images(ImageIds=[image])["Images"][0]["BlockDeviceMappings"][0]["Ebs"]["SnapshotId"]
    backend.snapshots[ami_snapshot].start_time = NOW - timedelta(days=200)
    running, stopped = [i["InstanceId"] for i in ec2.run_instances(
        ImageId=image, MinCount=2, MaxCount=2, SubnetId=subnet_a, SecurityGroupIds=[app_sg])["Instances"]]

    # Volumes: one attached to an instance, two left over.
    in_use = ec2.create_volume(Size=50, AvailabilityZone="us-east-1a", VolumeType="gp3",
                               TagSpecifications=name("volume", "orders-db-data"))["VolumeId"]
    ec2.attach_volume(VolumeId=in_use, InstanceId=running, Device="/dev/sdf")
    orphan = ec2.create_volume(Size=500, AvailabilityZone="us-east-1a", VolumeType="gp2",
                               TagSpecifications=name("volume", "old-reports-export"))["VolumeId"]
    ec2.create_volume(Size=100, AvailabilityZone="us-east-1b", VolumeType="gp3",
                      TagSpecifications=name("volume", "migration-scratch"))

    # Snapshots: an old forgotten one and a recent nightly backup.
    forgotten = ec2.create_snapshot(VolumeId=orphan, Description="before 2025 migration",
                                    TagSpecifications=name("snapshot", "reports-pre-migration"))["SnapshotId"]
    backend.snapshots[forgotten].start_time = NOW - timedelta(days=412)
    ec2.create_snapshot(VolumeId=in_use, Description="nightly backup",
                        TagSpecifications=name("snapshot", "orders-db-nightly"))

    # Elastic IPs: one idle, two serving NAT gateways.
    ec2.allocate_address(Domain="vpc", TagSpecifications=name("elastic-ip", "old-bastion"))
    busy_eip = ec2.allocate_address(Domain="vpc")["AllocationId"]
    idle_eip = ec2.allocate_address(Domain="vpc")["AllocationId"]

    # NAT gateways: one carrying traffic, one that has been silent for weeks.
    busy_nat = ec2.create_nat_gateway(SubnetId=subnet_a, AllocationId=busy_eip,
                                      TagSpecifications=name("natgateway", "prod-egress"))["NatGateway"]["NatGatewayId"]
    idle_nat = ec2.create_nat_gateway(SubnetId=subnet_b, AllocationId=idle_eip,
                                      TagSpecifications=name("natgateway", "staging-egress"))["NatGateway"]["NatGatewayId"]
    for nat_id in (busy_nat, idle_nat):
        backend.nat_gateways[nat_id]._created_at = NOW - timedelta(days=120)
    cloudwatch.put_metric_data(Namespace="AWS/NATGateway", MetricData=[{
        "MetricName": "BytesOutToDestination", "Value": 48_000_000_000.0,
        "Dimensions": [{"Name": "NatGatewayId", "Value": busy_nat}],
        "Timestamp": NOW - timedelta(hours=6),
    }])

    # Load balancers: one healthy, one whose only target is stopped, one with no targets.
    def load_balancer(lb_name: str, targets: list[str]) -> None:
        arn = elbv2.create_load_balancer(Name=lb_name, Subnets=[subnet_a, subnet_b],
                                         Type="application")["LoadBalancers"][0]["LoadBalancerArn"]
        group = elbv2.create_target_group(Name=f"{lb_name}-tg", Protocol="HTTP", Port=80,
                                          VpcId=vpc)["TargetGroups"][0]["TargetGroupArn"]
        elbv2.create_listener(LoadBalancerArn=arn, Protocol="HTTP", Port=80,
                              DefaultActions=[{"Type": "forward", "TargetGroupArn": group}])
        if targets:
            elbv2.register_targets(TargetGroupArn=group, Targets=[{"Id": t} for t in targets])

    load_balancer("shop-web", [running])
    load_balancer("batch-internal", [stopped])
    load_balancer("demo-2025", [])
    ec2.stop_instances(InstanceIds=[stopped])


def seed_eu_west_1(session) -> None:
    ec2 = session.client("ec2", region_name="eu-west-1")
    ec2.create_volume(Size=20, AvailabilityZone="eu-west-1a", VolumeType="gp3",
                      TagSpecifications=name("volume", "poc-data"))
    ec2.allocate_address(Domain="vpc", TagSpecifications=name("elastic-ip", "poc-ingress"))


def run() -> int:
    target = Path(__file__).resolve().parent / "snapshot.json"
    with mock_aws():
        session = boto3.Session(region_name="us-east-1")
        seed_us_east_1(session)
        seed_eu_west_1(session)
        return main(["scan", "--regions", "us-east-1,eu-west-1", "--save-snapshot", str(target)],
                    session=session)


if __name__ == "__main__":
    sys.exit(run())
