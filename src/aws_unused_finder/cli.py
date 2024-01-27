"""Command-line interface for aws-unused-finder."""

from __future__ import annotations

import argparse
import json
import sys

from . import __version__
from .analyze import analyze
from .collect import ALL_CHECKS, SCHEMA_VERSION, collect
from .guard import required_iam_actions
from .pricing import load_prices
from .report import render_json, render_text


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aws-unused-finder",
        description="Find AWS resources you pay for but do not use. Read-only: it never deletes or changes anything.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    scan = sub.add_parser("scan", help="scan regions (or a saved snapshot) and report unused resources")
    source = scan.add_mutually_exclusive_group()
    source.add_argument("--regions", metavar="R1,R2", help="comma-separated regions to scan; "
                        "defaults to the region of the active profile")
    source.add_argument("--from-file", metavar="FILE",
                        help="analyse a snapshot saved with --save-snapshot instead of calling AWS")
    scan.add_argument("--profile", help="AWS profile name (default: the standard credential chain)")
    scan.add_argument("--checks", metavar="LIST", default=",".join(ALL_CHECKS),
                      help=f"comma-separated checks to run (default: {','.join(ALL_CHECKS)})")
    scan.add_argument("--snapshot-age-days", type=int, default=90, metavar="N",
                      help="report snapshots older than N days (default: 90)")
    scan.add_argument("--idle-days", type=int, default=14, metavar="N",
                      help="a NAT gateway is idle if it carried no bytes for N days (default: 14)")
    scan.add_argument("--prices", metavar="FILE", help="JSON file overriding the default price table")
    scan.add_argument("--save-snapshot", metavar="FILE",
                      help="also write the collected data to FILE for later --from-file runs")
    scan.add_argument("--json", action="store_true", help="print JSON instead of a table")

    sub.add_parser("iam-policy", help="print the minimal IAM policy the scanner needs")
    return parser


def _iam_policy() -> str:
    return json.dumps({
        "Version": "2012-10-17",
        "Statement": [{"Sid": "AwsUnusedFinderReadOnly", "Effect": "Allow",
                       "Action": required_iam_actions(), "Resource": "*"}],
    }, indent=2)


def _load_snapshot(path: str) -> dict:
    with open(path, encoding="utf-8") as handle:
        snapshot = json.load(handle)
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("regions"), dict):
        raise ValueError(f"{path}: not a snapshot written by --save-snapshot")
    if snapshot.get("schema") != SCHEMA_VERSION:
        raise ValueError(f"{path}: unsupported snapshot schema {snapshot.get('schema')!r}")
    return snapshot


def main(argv: list[str] | None = None, session=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "iam-policy":
        print(_iam_policy())
        return 0

    checks = [c.strip() for c in args.checks.split(",") if c.strip()]
    unknown = sorted(set(checks) - set(ALL_CHECKS))
    if unknown:
        parser.error(f"unknown check(s): {', '.join(unknown)}; choose from {', '.join(ALL_CHECKS)}")
    if args.snapshot_age_days < 0 or args.idle_days < 1:
        parser.error("--snapshot-age-days must be >= 0 and --idle-days must be >= 1")

    try:
        prices = load_prices(args.prices)
        if args.from_file:
            snapshot = _load_snapshot(args.from_file)
            # honour --checks offline too
            keep = {"ebs": ["volumes"], "snapshots": ["snapshots", "ami_snapshot_ids"],
                    "eip": ["addresses"], "elb": ["load_balancers"],
                    "sg": ["security_groups", "eni_group_ids"], "nat": ["nat_gateways"]}
            wanted = {key for check in checks for key in keep[check]} | {"errors"}
            snapshot = {**snapshot, "regions": {
                region: {k: v for k, v in data.items() if k in wanted}
                for region, data in snapshot["regions"].items()}}
        else:
            if session is None:
                import boto3
                session = boto3.Session(profile_name=args.profile)
            regions = ([r.strip() for r in args.regions.split(",") if r.strip()]
                       if args.regions else [session.region_name] if session.region_name else [])
            if not regions:
                parser.error("no region: pass --regions or configure a default region")
            snapshot = collect(session, regions, checks, args.idle_days)
            if args.save_snapshot:
                with open(args.save_snapshot, "w", encoding="utf-8") as handle:
                    json.dump(snapshot, handle, indent=2)
                    handle.write("\n")
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # credential and endpoint problems from botocore
        if type(exc).__module__.startswith("botocore"):
            print(f"error: {exc}", file=sys.stderr)
            return 2
        raise

    findings = analyze(snapshot, prices, args.snapshot_age_days)
    print(render_json(snapshot, findings) if args.json else render_text(snapshot, findings))
    return 0


if __name__ == "__main__":
    sys.exit(main())
