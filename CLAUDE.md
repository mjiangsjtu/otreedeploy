# CLAUDE.md

Guidance for working in this repository.

## What this is

A deployment manager for oTree experiments on **one** lab server. Upload an
`.otreezip`, get a running experiment. One machine, one lab: experiments serve on port
80, this app manages them on port 8000.

The target is **any Debian/Ubuntu machine with systemd** -- LXC container, VM, cloud VPS
or bare metal, all identical. Written on Proxmox, but do not reintroduce Proxmox
assumptions (`pct` and friends were removed on purpose). systemd is a hard requirement,
not an implementation detail: experiments are units and the live log view reads the
journal, so a Docker-style app container is out of scope.

It is not a general PaaS and should not grow into one. It serves one lab, but a lab
with several groups whose data-protection promises differ (JBEF review, decision D2=C):
**each project's code runs as its own user and owns its own database role**, so no
project can reach another's data. The management password is still shared; per-person
accounts with per-project access are being added on top of that boundary.

## Running it

```bash
uv venv && uv pip install -e ".[dev]"

# Dry run: no root, no PostgreSQL, no systemd needed.
OTREEDEPLOY_ROOT=/tmp/otreedeploy-dev OTREEDEPLOY_DRY_RUN=1 \
  .venv/bin/otreedeploy --host 127.0.0.1 --port 8123

# A dry run with no accounts is open (as the built-in user "dev", an admin).
# To try accounts, create one against the same root:
OTREEDEPLOY_ROOT=/tmp/otreedeploy-dev .venv/bin/otreedeploy-users add ada --admin

.venv/bin/python -m pytest                # everything
.venv/bin/python -m pytest -m "not slow"  # skip real builds and boots
```

### Environment variables

| Variable | Effect |
|---|---|
| `OTREEDEPLOY_ROOT` | Prefix for every path. Defaults to `/`. Tests point it at a tmpdir. |
| `OTREEDEPLOY_DRY_RUN` | SQLite instead of PostgreSQL; systemd simulated. |
| `OTREEDEPLOY_OTREE_PORT` | The live experiment's port (default 80). In a dry run, setting it also makes the simulated supervisor **really** boot the experiment there. |
| `OTREEDEPLOY_PORT` | Read by `bootstrap.sh` only, to change the management port. |
| `OTREEDEPLOY_PASSWORD` | Read by `bootstrap.sh` only: the first account's (`admin`) password, without a prompt. |

## What dry run does and does not cover

Dry run is not a set of stubs. Archives are really extracted, `uv` really builds
virtualenvs, oTree really starts, sessions are really created through its REST API.

Three things are substituted, and each has concealed real bugs in the past:

- **PostgreSQL becomes SQLite.** SQLite has no roles, so nothing about database
  ownership, peer authentication or `GRANT` is exercised. The
  "permission denied for table otree_session" bug lived entirely in this gap.
- **No per-project users.** `sandbox.py` runs build, resetdb and boots locally as the
  current user. Who the code runs as is only checked by `tests/e2e`.
- **systemd becomes an in-memory pair of sets.** Unless `OTREEDEPLOY_OTREE_PORT` is
  set, "make this live" changes a flag and no process is launched.

When touching any of these, say plainly that the unit tests cannot confirm it, and run
the end-to-end suite (`tests/e2e/README.md`), which uses real systemd, PostgreSQL, sudo
and `bootstrap.sh` in a container.

Accounts are **not** in that gap. `accounts.py` and `access.py` are ordinary Python with
nothing substituted, and `tests/test_accounts.py` drives every route as every kind of
user through the real HTTP stack. The audit trail is: a dry run writes `audit.log`
instead of the journal, so the journal path, and `bootstrap.sh`'s account step, are
only covered by `tests/e2e`.

## Layout

```
otreedeploy/
  app.py          FastAPI routes, template wiring, i18n and auth context
  deploy.py       the pipeline: extract → build → resetdb → smoke test → publish
  archive.py      .otreezip validation (path traversal, symlinks, setuid)
  builder.py      uv venv construction, requirements normalisation
  postgres.py     per-project databases, ownership, backups
  systemd.py      unit control via otree-ctl; the dry-run stand-in lives here
  otreeapi.py     booting a project ourselves + oTree's REST API
  auth.py         the one password guarding this app
  health.py       readiness probing
  i18n.py         the English/Chinese catalogue
  config.py       paths and tunables; every path derives from OTREEDEPLOY_ROOT
deploy/           systemd units, otree-ctl, sudoers rule
templates/        Jinja2; no build step, no CDN, no framework
```

## Invariants worth knowing

These are load-bearing. Breaking one is how this app loses somebody's data.

1. **Re-uploading never drops a database.** `resetdb` runs only when
   `has_otree_tables()` is false.
2. **Uploaded code never runs as this app.** Building the venv (sdist hooks), `resetdb`,
   the smoke test, export boots and the live unit all run as `otree_<slug>`
   (`projects.project_user`) through `sandbox.py` -> `otree-ctl`, under the same
   hardening. Consequently everything in a project database is owned by that project's
   role, by construction. Never call a release's `.venv/bin/python` from the app --
   `builder.otree_version` reads package metadata instead.
3. **`DATABASE_URL` is always written explicitly.** Without it oTree silently falls back
   to a SQLite file inside the release directory, so a study appears to work while
   writing data the next deploy discards.
4. **Nothing serves HTTP as root.** The app's entire root surface is
   `/usr/local/sbin/otree-ctl`, which validates every argument (slug, release name,
   port range, canonical paths) before acting; the sudoers rule grants that one command
   with no wildcards. A new verb needs tests in `tests/test_otree_ctl.py` for both what
   it runs and what it refuses. Never use `REASSIGN OWNED` there: it also moves shared
   objects such as other projects' databases.
5. **`current` is repointed atomically**, via a temporary symlink and `rename`.
6. **A project's port is never hardcoded.** Templates render `otree_port()`. A test
   fails if any template contains a literal "port 80".
7. **Every route has an access rule, or it is closed.** `access.RULES` maps each route
   to public / signed-in / member of `{slug}` / admin, enforced by `access.EnforceAccess`
   before the route runs; a route without a rule answers 500, and
   `test_every_route_has_an_access_rule` fails. Accounts guard the management app and
   nothing else: port 80 must stay open or participants cannot take part.
8. **Accounts fail closed.** A real install with no account, or with an old password
   file that cannot be migrated, answers 503 everywhere; only a dry run with no
   accounts is open.
9. **Readiness probing never raises.** A non-HTTP reply means "not ready", not a crash —
   including the TCP self-connect a loopback probe can hit. `EPHEMERAL_PORT_RANGE` sits
   below the kernel's ephemeral range so that cannot happen in the first place.

## Accounts

Each person signs in as themselves (`accounts.py`); a project's members can do
everything with it and nobody else can see it (one level, by design); admins see every
project and manage accounts. Stored in `/var/lib/otreedeploy/accounts.db` (0600) --
deliberately not in `state.db`, which gets copied around during debugging.

- Sessions are server-side (only a hash of the cookie is stored), so signing out,
  disabling an account or changing a password ends sessions at once.
- An install from before accounts had one password in `auth.json`; `accounts.init()`
  turns it into the admin account `admin` with the same hash and renames the file.
- The first account is created on the server (`bootstrap.sh`, or `otreedeploy-users
  add`), never through the web: a server with no account is locked, not open.
- Uploading a new project makes you a member. A file named after somebody else's
  project becomes a separate project rather than touching theirs.
- Forcing an activation past the live project's active participants needs membership
  of *that* project.
- Every action that touches a project or its data goes to the audit trail
  (`audit.py`, the system journal), filtered on this app's own uid when read back.
  Use `_audit(request, ...)` in a new route that does anything.
- A background request (`Accept: text/event-stream`) gets 401, not a redirect.
- Failed sign-ins lock that address *and name* out for a doubling interval; the lockout
  applies to the right password too.

## Bilingual UI

English and Simplified Chinese, English the default. The catalogue is a plain dict in
`i18n.py` keyed by English source text — no gettext, no compile step, and an
untranslated string degrades to English rather than showing a marker.

- Templates call `t("English source text", **params)`. Placeholders are `{named}`.
- `t` always supplies `port=`, since many strings want it; `str.format` ignores a
  parameter the text does not mention.
- Adding a string means adding the English to the template and one entry to `ZH`. A test
  fails if a translation drops a placeholder.
- **Scope is the interface.** Build logs and the journal stream stay in English — they
  are uv's, oTree's and systemd's own words, and translating them would misrepresent
  what the tools actually said.
- The choice lives in a cookie. `Accept-Language` is deliberately ignored: the default
  is English for everyone, and Chinese is switched to on purpose.
- A message assembled in Python that carries a **value** must be translated there, via
  `app._msg` — by the time a template sees it the value is substituted and no catalogue
  key matches. A plain notice ("restarted") travels as English and is looked up on
  arrival. `{port}` is the exception: `t` always supplies it, so a template can fill it.

## Conventions

- **No new dependencies without a reason that survives scrutiny.** No CSS framework, no
  CDN, no build step. `templates/` and `static/style.css` are hand-written.
- Comments explain *why*, especially where the code looks odd — `otree prodserver`
  re-invoking itself by bare name, the process-group kill, the requirements rewriting.
  Do not strip these.
- User-facing text says what happened and what to do next. Compare the tone in
  `deploy.py` error messages before writing new ones.
- Tests are named as sentences about behaviour, not after the function under test.
- `slow` marks tests that really build a venv or boot oTree. They are the ones that
  would have caught a broken deploy; do not let them rot.

## Before saying something works

- Run the full suite, not just `-m "not slow"`.
- If the change touches PostgreSQL roles or systemd, say that dry run cannot confirm it
  and give the command to check on the container.
- `bootstrap.sh` is the upgrade path as well as the installer. It must stay idempotent,
  and it must restart `otreedeploy.service` — `enable --now` leaves old code running.
  Re-running it keeps an existing password rather than asking again.
