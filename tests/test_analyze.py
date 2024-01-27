from aws_unused_finder.analyze import analyze
from aws_unused_finder.pricing import DEFAULT_PRICES

NOW = "2026-06-01T00:00:00+00:00"


def run(region_data, prices=None, **kw):
    snapshot = {"schema": 1, "collected_at": NOW, "regions": {"us-east-1": region_data}}
    return analyze(snapshot, prices or DEFAULT_PRICES, **kw)


def volume(vid, state, size=100, vtype="gp3"):
    return {"VolumeId": vid, "Name": "", "Size": size, "VolumeType": vtype, "State": state}


def test_only_available_volumes_are_reported_and_priced_by_type():
    found = run({"volumes": [volume("vol-1", "in-use"), volume("vol-2", "available", 100, "gp3"),
                             volume("vol-3", "available", 200, "sc1"), volume("vol-4", "creating")]})
    assert [(f.resource_id, f.monthly_cost) for f in found] == [("vol-2", 8.0), ("vol-3", 3.0)]


def test_unknown_volume_type_falls_back_to_gp2_price():
    found = run({"volumes": [volume("vol-1", "available", 10, "future-type")]})
    assert found[0].monthly_cost == 1.0


def snapshot(sid, start, size=50, tier="standard"):
    return {"SnapshotId": sid, "Name": "", "VolumeSize": size, "StartTime": start, "StorageTier": tier}


def test_snapshot_age_threshold_and_ami_exclusion():
    data = {"snapshots": [
        snapshot("snap-old", "2025-01-01T00:00:00+00:00"),
        snapshot("snap-ami", "2025-01-01T00:00:00+00:00"),
        snapshot("snap-new", "2026-05-20T00:00:00+00:00"),
        snapshot("snap-edge", "2026-03-03T00:00:00+00:00"),  # exactly 90 days
    ], "ami_snapshot_ids": ["snap-ami"]}
    found = run(data)
    assert [f.resource_id for f in found] == ["snap-edge", "snap-old"]
    assert all(f.cost_is_upper_bound and f.monthly_cost == 2.5 for f in found)
    assert [f.resource_id for f in run(data, snapshot_age_days=365)] == ["snap-old"]
    assert len(run(data, snapshot_age_days=0)) == 3


def test_archived_snapshot_uses_archive_price_and_z_suffix_times_parse():
    found = run({"snapshots": [snapshot("snap-a", "2024-01-01T00:00:00Z", 1000, "archive")]})
    assert found[0].monthly_cost == 12.5


def test_elastic_ip_is_idle_only_without_any_association():
    found = run({"addresses": [
        {"AllocationId": "eipalloc-idle", "PublicIp": "203.0.113.10", "AssociationId": ""},
        {"AllocationId": "eipalloc-used", "PublicIp": "203.0.113.11", "AssociationId": "eipassoc-1"},
        {"AllocationId": "", "PublicIp": "203.0.113.12", "AssociationId": "", "InstanceId": "i-1"},
    ]})
    assert [(f.resource_id, f.monthly_cost) for f in found] == [("eipalloc-idle", 3.65)]


def lb(name, groups, kind="application", state="active"):
    return {"LoadBalancerArn": f"arn:aws:elasticloadbalancing:us-east-1:123456789012:loadbalancer/app/{name}/1",
            "LoadBalancerName": name, "Type": kind, "State": state,
            "TargetGroups": [{"TargetGroupName": f"tg{i}", "TargetStates": s} for i, s in enumerate(groups)]}


def test_load_balancer_reasons():
    found = {f.resource_id: f for f in run({"load_balancers": [
        lb("no-groups", []),
        lb("no-targets", [[], []]),
        lb("all-bad", [["unhealthy", "draining"], ["unused"]]),
        lb("one-good", [["unhealthy"], ["healthy"]]),
        lb("lambda-target", [["unavailable"]]),
        lb("still-provisioning", [], state="provisioning"),
        lb("gateway", [], kind="gateway"),
    ]})}
    assert set(found) == {"no-groups", "no-targets", "all-bad", "gateway"}
    assert "no target groups" in found["no-groups"].reason
    assert "2 target group(s), no registered targets" in found["no-targets"].reason
    assert "0 of 3 registered target(s) healthy" in found["all-bad"].reason
    assert found["no-groups"].monthly_cost == 16.43 and found["gateway"].monthly_cost == 9.12


def test_security_groups_skip_default_and_attached_and_show_references():
    found = {f.resource_id: f for f in run({
        "security_groups": [
            {"GroupId": "sg-default", "GroupName": "default", "ReferencesGroupIds": []},
            {"GroupId": "sg-used", "GroupName": "web", "ReferencesGroupIds": ["sg-orphan"]},
            {"GroupId": "sg-orphan", "GroupName": "old", "ReferencesGroupIds": []},
            {"GroupId": "sg-lonely", "GroupName": "lonely", "ReferencesGroupIds": []},
        ],
        "eni_group_ids": ["sg-used"],
    })}
    assert set(found) == {"sg-orphan", "sg-lonely"}
    assert "still referenced in rules of sg-used" in found["sg-orphan"].reason
    assert found["sg-lonely"].monthly_cost == 0


def nat(nid, traffic, created="2026-01-01T00:00:00+00:00", state="available"):
    return {"NatGatewayId": nid, "Name": "", "State": state, "CreateTime": created,
            "TrafficBytes": traffic, "TrafficDays": 14}


def test_nat_gateway_idle_rules():
    found = run({"nat_gateways": [
        nat("nat-idle", 0.0),
        nat("nat-busy", 1024.0),
        nat("nat-new", 0.0, created="2026-05-25T00:00:00+00:00"),  # younger than the window
        nat("nat-unknown", None),                                   # metrics could not be read
        nat("nat-deleted", 0.0, state="deleted"),
    ]})
    assert [(f.resource_id, f.monthly_cost) for f in found] == [("nat-idle", 32.85)]


def test_price_overrides_change_estimates():
    prices = {**DEFAULT_PRICES, "ebs_gb_month.gp3": 0.5, "public_ipv4_hour": 0.01}
    found = run({"volumes": [volume("vol-1", "available", 10)],
                 "addresses": [{"AllocationId": "eipalloc-1", "PublicIp": "203.0.113.1", "AssociationId": ""}]},
                prices=prices)
    assert {f.resource_id: f.monthly_cost for f in found} == {"vol-1": 5.0, "eipalloc-1": 7.3}


def test_findings_sorted_by_region_then_cost():
    snapshot = {"collected_at": NOW, "regions": {
        "us-east-1": {"volumes": [volume("vol-small", "available", 1), volume("vol-big", "available", 900)]},
        "eu-west-1": {"volumes": [volume("vol-eu", "available", 5)]},
    }}
    assert [f.resource_id for f in analyze(snapshot, DEFAULT_PRICES)] == ["vol-eu", "vol-big", "vol-small"]


def test_empty_region_has_no_findings():
    assert run({}) == []
