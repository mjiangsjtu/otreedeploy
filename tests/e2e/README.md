# End-to-end tests

Everything that dry-run mode substitutes is real here: Ubuntu 24.04 with systemd as
PID 1, PostgreSQL with peer authentication, sudo and `otree-ctl`, and `bootstrap.sh`
run as the README documents it. Each test gets a freshly booted copy of the installed
server, so tests cannot affect one another.

```bash
# needs Docker (or Podman: OTREEDEPLOY_E2E_RUNTIME=podman) and network access
OTREEDEPLOY_E2E=1 .venv/bin/python -m pytest tests/e2e -v -rxX
OTREEDEPLOY_E2E=1 .venv/bin/python -m pytest tests/e2e -k restore   # one scenario
OTREEDEPLOY_E2E_KEEP=1 ...                                          # keep containers to inspect
```

Tests marked `xfail(strict=True)` reproduce a known defect and name the review item.
When a fix lands, the test XPASSes, which fails the run until the marker is removed.

`e2e-metrics.json` records the install time, upload-to-built time, activation time,
reboot recovery and the exact platform versions, for reporting.
