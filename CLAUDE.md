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

It is not a general PaaS and should not grow into one. Every design choice assumes a
single trusted lab: one password for the whole management app, no user accounts, no
multi-tenancy, no per-user permissions.

## Running it

```bash
uv venv && uv pip install -e ".[dev]"

# Dry run: no root, no PostgreSQL, no systemd needed.
OTREEDEPLOY_ROOT=/tmp/otreedeploy-dev OTREEDEPLOY_DRY_RUN=1 \
  .venv/bin/otreedeploy --host 127.0.0.1 --port 8123

# A dry run has no password until one is set against the same root.
OTREEDEPLOY_ROOT=/tmp/otreedeploy-dev .venv/bin/otreedeploy-passwd

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
| `OTREEDEPLOY_PASSWORD` | Read by `bootstrap.sh` only, to set the password without a prompt. |

## What dry run does and does not cover

Dry run is not a set of stubs. Archives are really extracted, `uv` really builds
virtualenvs, oTree really starts, sessions are really created through its REST API.

Two things are substituted, and both have concealed real bugs in the past:

- **PostgreSQL becomes SQLite.** SQLite has no roles, so nothing about database
  ownership, peer authentication or `GRANT` is exercised. The
  "permission denied for table otree_session" bug lived entirely in this gap.
- **systemd becomes an in-memory pair of sets.** Unless `OTREEDEPLOY_OTREE_PORT` is
  set, "make this live" changes a flag and no process is launched.

When touching either area, say plainly in the response that the tests cannot confirm it.

The password is **not** in that gap. `auth.py` is ordinary Python with nothing
substituted, and `tests/test_auth.py` drives it through the real HTTP stack, so a claim
that the login works is a claim the tests actually support. What they do not cover is
`bootstrap.sh` itself — the prompt loop, `su`-ing to the app user, the resulting file
mode. Say so, and give the `ls -l /var/lib/otreedeploy/auth.json` check.

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
2. **Everything in a project database is owned by the `otree` role.** The app runs
   `resetdb` as its own role, so `postgres.align_ownership()` hands the tables over
   afterwards. The experiment connects as `otree` and would otherwise be denied.
3. **`DATABASE_URL` is always written explicitly.** Without it oTree silently falls back
   to a SQLite file inside the release directory, so a study appears to work while
   writing data the next deploy discards.
4. **Nothing serves HTTP as root.** The app's entire root surface is
   `/usr/local/sbin/otree-ctl`, which validates its own arguments; the sudoers rule
   grants that one command with no wildcards.
5. **`current` is repointed atomically**, via a temporary symlink and `rename`.
6. **A project's port is never hardcoded.** Templates render `otree_port()`. A test
   fails if any template contains a literal "port 80".
7. **The password guards the management app and nothing else.** Port 80 must stay open
   or participants cannot take part. `auth.RequireLogin` wraps the whole app so a new
   route is protected by default; `auth.PUBLIC_PATHS` is the short list of exceptions.
8. **An unreadable credential file locks the app, never opens it.** "No file" means no
   password was ever set; a damaged one means 503.
9. **Readiness probing never raises.** A non-HTTP reply means "not ready", not a crash —
   including the TCP self-connect a loopback probe can hit. `EPHEMERAL_PORT_RANGE` sits
   below the kernel's ephemeral range so that cannot happen in the first place.

## The password

One password, no accounts. Set by `bootstrap.sh`, hashed with PBKDF2 into
`/var/lib/otreedeploy/auth.json` (0600) — deliberately not in `state.db`, which gets
copied around during debugging.

- **No password file at all means the app serves unprotected.** That is what keeps a dry
  run on a laptop friction-free, and it is why every page carries a warning banner until
  one is set. Do not "helpfully" make an unset password fail closed without also giving
  the developer workflow a way through.
- The session cookie is stateless: an expiry plus an HMAC over it, signed with a secret
  stored beside the hash. Changing the password rotates that secret, which is what signs
  every other browser out.
- The **first** password cannot be set through the web UI. Until one exists the app is
  open, so anyone on the network could claim it and lock the experimenter out.
- A background request (`Accept: text/event-stream`) gets 401, not a redirect: the two
  log streams cannot act on a login page delivered as though it were data.
- Failed sign-ins lock that address out for a doubling interval, and the lockout applies
  to the right password too — letting exactly one guess through would say which it was.
- The "no password is set" banner is suppressed in a dry run. That is intentional, not an
  oversight: the banner warns about an exposed lab server, and a dry run is a laptop.

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
