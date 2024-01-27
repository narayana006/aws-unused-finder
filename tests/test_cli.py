import json
from pathlib import Path

import pytest

from aws_unused_finder.cli import main

EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "snapshot.json"


def run(capsys, *argv, session=None):
    code = main(list(argv), session=session)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_example_snapshot_report(capsys):
    code, out, _ = run(capsys, "scan", "--from-file", str(EXAMPLE))
    assert code == 0
    assert "us-east-1: 9 unused resource(s)" in out and "eu-west-1: 2 unused resource(s)" in out
    assert "Total: 11 unused resource(s), estimated $132.61/month" in out
    assert "plus up to $25.00/month for snapshots" in out
    assert "orders-db" not in out and "prod-egress" not in out and "shop-web" not in out
    assert "Nothing was changed." in out


def test_example_snapshot_json_and_check_filter(capsys):
    code, out, _ = run(capsys, "scan", "--from-file", str(EXAMPLE), "--json", "--checks", "ebs,eip")
    data = json.loads(out)
    assert code == 0
    assert {f["check"] for f in data["findings"]} == {"ebs", "eip"}
    assert data["totals"] == {"findings": 5, "monthly_cost": 66.9, "snapshot_upper_bound": 0}


def test_snapshot_age_and_price_overrides(capsys, tmp_path):
    prices = tmp_path / "prices.json"
    prices.write_text(json.dumps({"nat_gateway_hour": 0.05}))
    code, out, _ = run(capsys, "scan", "--from-file", str(EXAMPLE), "--json", "--checks", "nat,snapshots",
                       "--snapshot-age-days", "500", "--prices", str(prices))
    data = json.loads(out)
    assert [(f["check"], f["monthly_cost"]) for f in data["findings"]] == [("nat", 36.5)]


def test_live_scan_against_mock_saves_snapshot_that_replays_identically(capsys, tmp_path, aws):
    ec2 = aws.client("ec2")
    ec2.create_volume(Size=10, AvailabilityZone="us-east-1a", VolumeType="gp3")
    ec2.allocate_address(Domain="vpc")
    saved = tmp_path / "snap.json"
    code, live, _ = run(capsys, "scan", "--regions", "us-east-1", "--save-snapshot", str(saved), session=aws)
    assert code == 0 and "us-east-1: 2 unused resource(s)" in live
    assert "estimated $4.45/month" in live
    code, replay, _ = run(capsys, "scan", "--from-file", str(saved))
    assert replay == live


def test_default_region_comes_from_session(capsys, aws):
    code, out, _ = run(capsys, "scan", session=aws)
    assert code == 0 and "Scanned 1 region(s): us-east-1" in out


def test_skipped_check_is_shown_as_warning(capsys, tmp_path):
    snap = tmp_path / "s.json"
    snap.write_text(json.dumps({"schema": 1, "collected_at": "2026-06-01T00:00:00+00:00", "regions": {
        "us-east-1": {"errors": [{"check": "nat", "error": "AccessDenied"}]}}}))
    code, out, _ = run(capsys, "scan", "--from-file", str(snap))
    assert code == 0 and "warning: us-east-1: check 'nat' was skipped: AccessDenied" in out


def test_iam_policy_command(capsys):
    code, out, _ = run(capsys, "iam-policy")
    policy = json.loads(out)
    assert code == 0 and len(policy["Statement"][0]["Action"]) == 11
    assert "ec2:DescribeVolumes" in policy["Statement"][0]["Action"]


def test_bad_inputs(capsys, tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("[]")
    assert run(capsys, "scan", "--from-file", str(bad))[0] == 2
    assert run(capsys, "scan", "--from-file", str(tmp_path / "missing.json"))[0] == 2
    prices = tmp_path / "p.json"
    prices.write_text(json.dumps({"made_up": 1}))
    code, _, err = run(capsys, "scan", "--from-file", str(EXAMPLE), "--prices", str(prices))
    assert code == 2 and "unknown price key" in err
    for argv in (["scan", "--checks", "rds"], ["scan", "--idle-days", "0"],
                 ["scan", "--regions", "us-east-1", "--from-file", str(EXAMPLE)], []):
        with pytest.raises(SystemExit) as exc:
            main(argv)
        assert exc.value.code == 2


def test_source_has_no_mutating_api_calls():
    source = Path(__file__).resolve().parent.parent / "src" / "aws_unused_finder"
    text = "\n".join(p.read_text() for p in source.glob("*.py"))
    for verb in ("delete_", "terminate_", "release_", "deregister_", "detach_", "modify_", "revoke_"):
        assert f".{verb}" not in text
