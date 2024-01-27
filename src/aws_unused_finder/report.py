"""Render findings as a text table or JSON."""

from __future__ import annotations

import json

from .analyze import Finding

_CHECK_LABELS = {
    "ebs": "EBS volume", "snapshots": "EBS snapshot", "eip": "Elastic IP",
    "elb": "Load balancer", "sg": "Security group", "nat": "NAT gateway",
}


def totals(findings: list[Finding]) -> dict:
    firm = sum(f.monthly_cost for f in findings if not f.cost_is_upper_bound)
    upper = sum(f.monthly_cost for f in findings if f.cost_is_upper_bound)
    return {"findings": len(findings), "monthly_cost": round(firm, 2),
            "snapshot_upper_bound": round(upper, 2)}


def _cost(finding: Finding) -> str:
    if finding.monthly_cost == 0:
        return "-"
    return f"{'<=' if finding.cost_is_upper_bound else ''}${finding.monthly_cost:,.2f}"


def _errors(snapshot: dict) -> list[tuple[str, dict]]:
    return [(region, error) for region, data in snapshot.get("regions", {}).items()
            for error in data.get("errors", [])]


def render_text(snapshot: dict, findings: list[Finding]) -> str:
    lines = []
    regions = list(snapshot.get("regions", {}))
    lines.append(f"Scanned {len(regions)} region(s): {', '.join(regions)}  "
                 f"(collected {snapshot.get('collected_at', 'unknown')})")
    for region in regions:
        rows = [f for f in findings if f.region == region]
        lines.append("")
        lines.append(f"{region}: {len(rows)} unused resource(s)")
        if not rows:
            continue
        table = [("TYPE", "RESOURCE", "NAME", "USD/MONTH", "WHY")] + [
            (_CHECK_LABELS.get(f.check, f.check), f.resource_id, f.name or "-", _cost(f), f.reason)
            for f in rows
        ]
        widths = [max(len(row[i]) for row in table) for i in range(4)]
        for row in table:
            lines.append("  " + "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row[:4]))
                         + "  " + row[4])
    summary = totals(findings)
    lines.append("")
    lines.append(f"Total: {summary['findings']} unused resource(s), "
                 f"estimated ${summary['monthly_cost']:,.2f}/month")
    if summary["snapshot_upper_bound"]:
        lines.append(f"       plus up to ${summary['snapshot_upper_bound']:,.2f}/month for snapshots "
                     "(upper bound: snapshots are incremental and billed on stored blocks)")
    for region, error in _errors(snapshot):
        lines.append(f"warning: {region}: check '{error['check']}' was skipped: {error['error']}")
    lines.append("Nothing was changed. This tool only reads; review each resource before removing it yourself.")
    return "\n".join(lines)


def render_json(snapshot: dict, findings: list[Finding]) -> str:
    return json.dumps({
        "collected_at": snapshot.get("collected_at"),
        "regions": list(snapshot.get("regions", {})),
        "totals": totals(findings),
        "findings": [f.to_dict() for f in findings],
        "errors": [{"region": region, **error} for region, error in _errors(snapshot)],
    }, indent=2)
