# aws-unused-finder

Find AWS resources you are paying for but not using: unattached volumes, idle IPs, old snapshots, empty load balancers and more.

> **Project status: planning. There is no working code in this repository yet.**
> This README describes what the project will do. Nothing below can be installed or run today.
> Features are ticked off in the checklist as they are built.

## What it is

A command-line tool.

**The problem it solves:** Unused resources pile up in every AWS account and quietly add to the bill.

## Who it is for

Account owners and platform teams doing cost clean-ups.

## How it will work

1. You run a scan across the regions you choose.
2. The tool lists resources and checks whether each one is attached or receiving traffic.
3. It prints the unused ones with an estimated monthly cost.

## Planned features

Unticked items are not built yet.

- [ ] Unattached EBS volumes and old snapshots
- [ ] Elastic IPs with no association
- [ ] Load balancers with no healthy targets
- [ ] Unused security groups and NAT gateways with no traffic
- [ ] Estimated monthly cost for each finding
- [ ] Read-only: it reports and never deletes

## Planned usage

This is the intended interface, shown so you can judge the design. **It does not work yet**, and names may change before the first release.

```bash
aws-unused-finder scan --regions us-east-1,us-west-2
```

## Contributing

Feedback on the design is the most useful contribution right now. Open an issue to suggest a feature, question an approach or describe your use case. Code contributions are welcome once the first version lands.

## Author

Built and maintained by [Narayana Vemburaj](https://github.com/narayana006).

## License

[MIT](LICENSE)
