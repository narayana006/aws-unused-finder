"""Hard allow-list of AWS API operations.

Every client the scanner creates goes through `guarded_client`. A request for
any operation outside ALLOWED_OPERATIONS is refused before it is sent, so the
tool cannot delete or change anything even if a future bug tried to.
"""

from __future__ import annotations

# service -> operations. These are also the IAM actions the scanner needs.
ALLOWED_OPERATIONS: dict[str, frozenset[str]] = {
    "ec2": frozenset({
        "DescribeVolumes", "DescribeSnapshots", "DescribeImages", "DescribeAddresses",
        "DescribeSecurityGroups", "DescribeNetworkInterfaces", "DescribeNatGateways",
    }),
    "elbv2": frozenset({"DescribeLoadBalancers", "DescribeTargetGroups", "DescribeTargetHealth"}),
    "cloudwatch": frozenset({"GetMetricStatistics"}),
}

# IAM uses a different service prefix for ELB than the boto3 client name.
_IAM_PREFIX = {"ec2": "ec2", "elbv2": "elasticloadbalancing", "cloudwatch": "cloudwatch"}


class ReadOnlyViolation(RuntimeError):
    """Raised when code tries to call an operation outside the allow-list."""


def required_iam_actions() -> list[str]:
    return sorted(f"{_IAM_PREFIX[service]}:{op}"
                  for service, ops in ALLOWED_OPERATIONS.items() for op in ops)


def guarded_client(session, service: str, region: str):
    if service not in ALLOWED_OPERATIONS:
        raise ReadOnlyViolation(f"service {service!r} is not on the read-only allow-list")
    client = session.client(service, region_name=region)
    allowed = ALLOWED_OPERATIONS[service]

    def refuse_unlisted(model, **_kwargs):
        if model.name not in allowed:
            raise ReadOnlyViolation(
                f"refusing to call {service}:{model.name}: not on the read-only allow-list")

    client.meta.events.register("before-call", refuse_unlisted)
    return client
