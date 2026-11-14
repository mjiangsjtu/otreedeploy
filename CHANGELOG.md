# Changelog

Versions follow [semantic versioning](https://semver.org). Upgrading is re-running
`bootstrap.sh` from the new version; each entry says if anything else is needed.

## 0.2.0 — unreleased

The revision in response to the JBEF review of 0.1.0 (commit `f7f62bc`). Upgrading
from 0.1.0 is re-running `bootstrap.sh`: it backs up every project's database, moves
each project to its own user and database role, and turns the management password
into the account `admin`. Do it between sessions: the live study is restarted.

**Security and data protection**
- Each project's code runs as its own system user and database role in every phase
  (build, schema, test-start, export, live), under systemd's sandboxing; projects
  cannot reach each other's data or the app's.
- Accounts: each experimenter signs in as themselves and sees only their projects;
  admins manage accounts. Sessions are server-side.
- Audit trail in the system journal: sign-ins, data and backup downloads, every action.
- Retention periods for backups and uploads; delete can purge all of a project's data.
- Optional HTTPS through Caddy (`OTREEDEPLOY_DOMAIN`); warnings when a plain-HTTP
  server has a public address.
- Forged form submissions from the experiment's pages are refused; untrusted text is
  never formatted; messages in URLs are signed; open redirect via `/\host` closed.

**Safety of running studies**
- Schema changes are compared with the database before going live: additions applied,
  incompatible changes refused.
- Restore a backup from the project page, all or nothing.
- A failed activation restores the previous live project; "started" means the
  project's own REST key is accepted.
- One action at a time per project; overrides on every guarded action; participants
  part-way through a session count as active for 30 minutes.
- The live study follows PostgreSQL restarts.

**Operation**
- Re-running `bootstrap.sh` works (upgrades); `uv` pinned to 0.13.8.
- Every release records its exact package versions beside the upload.
- End-to-end test suite on real systemd and PostgreSQL (`tests/e2e`).

Tested on Ubuntu 24.04.5, PostgreSQL 16.15, Python 3.12.3, systemd 255, uv 0.13.8,
oTree 6.0.15.

## 0.1.0 — 2026-09

First published version (commit `f7f62bc`), as submitted to JBEF.
