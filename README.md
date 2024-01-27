# aws-unused-finder

Find AWS resources you are paying for but not using: unattached EBS volumes, old snapshots, idle Elastic IPs, load balancers with no healthy targets, idle NAT gateways and unattached security groups, each with an estimated monthly cost.

**It is read-only. It reports and never deletes, stops or modifies anything.**

**Status: working first version (0.1.0).** All six checks below are implemented. They are tested against [moto](https://github.com/getmoto/moto), an in-memory AWS mock, and against botocore stubs. The tool has not yet been run against a real AWS account by the author, so treat the first run on your account as a trial and verify findings before acting on them.

## Install

```bash
pip install git+https://github.com/narayana006/aws-unused-finder
```

Python 3.10 or newer. The only runtime dependency is `boto3`.

## Try it without an AWS account

`examples/snapshot.json` is a saved scan of a small fake estate. It was produced by running the real collector against moto (see `examples/generate_snapshot.py`); the account id `123456789012`, the resource ids and the `127.x.x.x` addresses are moto's fakes. Analysing it needs no credentials:

```console
$ aws-unused-finder scan --from-file examples/snapshot.json
Scanned 2 region(s): us-east-1, eu-west-1  (collected 2026-11-10T14:26:30.455698+00:00)

us-east-1: 9 unused resource(s)
  TYPE            RESOURCE                    NAME                   USD/MONTH  WHY
  EBS volume      vol-4d53f02373f5d4f78       old-reports-export     $50.00     unattached gp2 volume, 500 GiB
  NAT gateway     nat-fc1afac50020f4705       staging-egress         $32.85     NAT gateway carried 0 bytes in the last 14 days
  EBS snapshot    snap-fcb3b42d133f81f38      reports-pre-migration  <=$25.00   snapshot is 412 days old, not used by any AMI you own, source volume 500 GiB
  Load balancer   batch-internal              -                      $16.43     application load balancer: 0 of 1 registered target(s) healthy
  Load balancer   demo-2025                   -                      $16.43     application load balancer: 1 target group(s), no registered targets
  EBS volume      vol-886eb33248892d148       migration-scratch      $8.00      unattached gp3 volume, 100 GiB
  Elastic IP      eipalloc-85f10f30b496b72ed  old-bastion            $3.65      Elastic IP 127.168.46.64 is not associated with anything
  Security group  sg-a47567c963cc40168        legacy-db              -          not attached to any network interface
  Security group  sg-f9d0c172973cde8bb        legacy-app             -          not attached to any network interface; still referenced in rules of sg-a47567c963cc40168

eu-west-1: 2 unused resource(s)
  TYPE        RESOURCE                    NAME         USD/MONTH  WHY
  Elastic IP  eipalloc-11bc4c238d5ab8e44  poc-ingress  $3.65      Elastic IP 127.20.86.63 is not associated with anything
  EBS volume  vol-1f4d30195cf8b57ff       poc-data     $1.60      unattached gp3 volume, 20 GiB

Total: 11 unused resource(s), estimated $132.61/month
       plus up to $25.00/month for snapshots (upper bound: snapshots are incremental and billed on stored blocks)
Nothing was changed. This tool only reads; review each resource before removing it yourself.
```

The estate also contains an attached volume, a recent snapshot, a 200-day-old snapshot that backs an AMI, a NAT gateway with traffic, a load balancer with a healthy target and a security group in use. None of those are listed.

## Scan a real account

```bash
aws-unused-finder scan --regions us-east-1,us-west-2
aws-unused-finder scan --regions eu-west-1 --profile audit --save-snapshot scan.json
aws-unused-finder scan --from-file scan.json --snapshot-age-days 30 --json
```

Credentials come from the standard boto3 chain (environment, shared config, SSO, instance role) or `--profile`. Without `--regions` the profile's default region is scanned.

| Option | Meaning |
| --- | --- |
| `--regions R1,R2` | Regions to scan. |
| `--from-file FILE` | Analyse a saved snapshot instead of calling AWS. |
| `--save-snapshot FILE` | Write the collected data so it can be re-analysed later with different thresholds or prices. |
| `--profile NAME` | AWS profile to use. |
| `--checks LIST` | Subset of `ebs,snapshots,eip,elb,sg,nat` (default: all). |
| `--snapshot-age-days N` | Report snapshots older than N days (default 90). |
| `--idle-days N` | NAT gateway traffic window in days (default 14). Applied when scanning; a saved snapshot keeps the window it was collected with. |
| `--prices FILE` | JSON file that overrides entries of the price table. |
| `--json` | JSON output. |

Exit code is 0 when the scan ran (with or without findings) and 2 on bad arguments, unreadable files or credential errors.

JSON output for two checks of the example:

```console
$ aws-unused-finder scan --from-file examples/snapshot.json --checks nat,eip --json
{
  "collected_at": "2026-11-10T14:26:30.455698+00:00",
  "regions": [
    "us-east-1",
    "eu-west-1"
  ],
  "totals": {
    "findings": 3,
    "monthly_cost": 40.15,
    "snapshot_upper_bound": 0
  },
  "findings": [
    {
      "region": "eu-west-1",
      "check": "eip",
      "resource_id": "eipalloc-11bc4c238d5ab8e44",
      "name": "poc-ingress",
      "reason": "Elastic IP 127.20.86.63 is not associated with anything",
      "monthly_cost": 3.65,
      "cost_is_upper_bound": false
    },
    {
      "region": "us-east-1",
      "check": "nat",
      "resource_id": "nat-fc1afac50020f4705",
      "name": "staging-egress",
      "reason": "NAT gateway carried 0 bytes in the last 14 days",
      "monthly_cost": 32.85,
      "cost_is_upper_bound": false
    },
    {
      "region": "us-east-1",
      "check": "eip",
      "resource_id": "eipalloc-85f10f30b496b72ed",
      "name": "old-bastion",
      "reason": "Elastic IP 127.168.46.64 is not associated with anything",
      "monthly_cost": 3.65,
      "cost_is_upper_bound": false
    }
  ],
  "errors": []
}
```

## What counts as unused

| Check | Reported when | Cost shown |
| --- | --- | --- |
| `ebs` | Volume state is `available` (attached to nothing). | Size x per-GB price for the volume type. Provisioned IOPS and throughput are not included. |
| `snapshots` | Snapshot owned by the account, older than `--snapshot-age-days`, and not referenced by the block device mapping of any AMI the account owns. | Shown as `<=`: source volume size x snapshot price. This is an upper bound, because snapshots are incremental and billed on stored blocks. Kept out of the main total. |
| `eip` | Elastic IP has no association, instance or network interface. | Public IPv4 hourly price x 730. |
| `elb` | Application, Network or Gateway Load Balancer in `active` state with no target groups, no registered targets, or no target in state `healthy`. Targets in state `unavailable` (health checks disabled, e.g. Lambda targets) count as serving. | Hourly price for the type x 730. Capacity-unit (LCU) charges are not included. |
| `sg` | Security group not attached to any network interface. Default groups are skipped. If another group's rules reference it, that is noted. | No charge. |
| `nat` | NAT gateway in `available` state, older than the window, with a sum of 0 across `BytesOutToDestination`, `BytesInFromDestination`, `BytesOutToSource` and `BytesInFromSource` over `--idle-days`. | Hourly price x 730. |

## Cost estimates

Defaults are the public on-demand list prices for US East (N. Virginia) from the AWS pricing pages for EBS, VPC (public IPv4 and NAT gateway) and Elastic Load Balancing, as written in `src/aws_unused_finder/pricing.py`. **The same table is applied to every region and it will go out of date**, so check it against current pricing and override what differs:

| Key | Default (USD) |
| --- | --- |
| `ebs_gb_month.gp3` | 0.08 |
| `ebs_gb_month.gp2` | 0.1 |
| `ebs_gb_month.io1` | 0.125 |
| `ebs_gb_month.io2` | 0.125 |
| `ebs_gb_month.st1` | 0.045 |
| `ebs_gb_month.sc1` | 0.015 |
| `ebs_gb_month.standard` | 0.05 |
| `snapshot_gb_month.standard` | 0.05 |
| `snapshot_gb_month.archive` | 0.0125 |
| `public_ipv4_hour` | 0.005 |
| `nat_gateway_hour` | 0.045 |
| `load_balancer_hour.application` | 0.0225 |
| `load_balancer_hour.network` | 0.0225 |
| `load_balancer_hour.gateway` | 0.0125 |

`*_gb_month` keys are per GB-month, `*_hour` keys are per hour; a month is 730 hours. Override any subset:

```bash
aws-unused-finder scan --from-file examples/snapshot.json --prices examples/prices-override.json
```

`examples/prices-override.json` only shows the file format; its numbers are placeholders, not verified prices. Discounts, Savings Plans, free tier and taxes are not modelled.

## IAM permissions

The scanner calls exactly these 11 actions and nothing else. `aws-unused-finder iam-policy` prints the policy:

```console
$ aws-unused-finder iam-policy
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "AwsUnusedFinderReadOnly",
      "Effect": "Allow",
      "Action": [
        "cloudwatch:GetMetricStatistics",
        "ec2:DescribeAddresses",
        "ec2:DescribeImages",
        "ec2:DescribeNatGateways",
        "ec2:DescribeNetworkInterfaces",
        "ec2:DescribeSecurityGroups",
        "ec2:DescribeSnapshots",
        "ec2:DescribeVolumes",
        "elasticloadbalancing:DescribeLoadBalancers",
        "elasticloadbalancing:DescribeTargetGroups",
        "elasticloadbalancing:DescribeTargetHealth"
      ],
      "Resource": "*"
    }
  ]
}
```

`--from-file` needs no permissions at all.

### How read-only is enforced

- Every AWS client is created through `guard.py`, which registers a hook that refuses any operation not on the allow-list above **before the request is sent**. A test asserts that `delete_volume`, `release_address` and even an unlisted `describe_instances` are refused.
- A test runs a full scan against the mock and asserts that only allow-listed operations were called and that no resource changed.
- There is no `--delete`, `--apply` or `--fix` option, and none is planned for this tool.

## Features

- [x] Unattached EBS volumes and old snapshots
- [x] Elastic IPs with no association
- [x] Load balancers with no healthy targets (ALB, NLB, GWLB)
- [x] Unused security groups and NAT gateways with no traffic
- [x] Estimated monthly cost for each finding
- [x] Read-only: it reports and never deletes
- [x] Offline analysis of saved snapshots (`--save-snapshot` / `--from-file`)
- [ ] Classic Load Balancers
- [ ] Scan every enabled region automatically
- [ ] Region-specific price tables
- [ ] Multi-account scans

## Limitations

- **"Unused" is a heuristic, not proof.** A detached volume may be a deliberate backup; a load balancer may be scaled to zero overnight. Review every finding.
- **Security groups:** only attachment to network interfaces is checked. A group referenced only by a launch template, launch configuration or a service currently scaled to zero has no network interface and is reported as unused.
- **Snapshots:** only AMIs owned by the scanned account are considered. Snapshots shared with other accounts, used by AMIs elsewhere, or managed by AWS Backup or Data Lifecycle Manager are not recognised as in use.
- **NAT gateways:** depends on CloudWatch metrics being available for the window. Gateways younger than the window are skipped.
- **Load balancers:** health is a point-in-time reading; request counts are not examined. Classic Load Balancers are not scanned.
- **Costs** are estimates from a single static price table (see above).
- If a check is denied or fails in a region, the scan continues and prints a `warning:` line naming the skipped check. Findings for that check are then missing, not zero.
- Verified against moto and botocore stubs only; moto does not reproduce every AWS behaviour.

## Development

```bash
git clone https://github.com/narayana006/aws-unused-finder
cd aws-unused-finder
pip install -e ".[dev]"
pytest
```

The tests set fake credentials and run entirely against moto and botocore's `Stubber`; they never contact AWS.

## Contributing

Issues and pull requests are welcome: false positives with a description of the resource, new read-only checks, and corrections to the price table with a link to the source. Changes must keep the tool read-only.

Built and maintained by [Narayana Vemburaj](https://github.com/narayana006).

## License

[MIT](LICENSE)
