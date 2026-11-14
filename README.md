# otreedeploy

Upload an `.otreezip`, get a running oTree experiment. A small deployment manager for a
single experimental-economics server — the Heroku experience, for oTree only, on hardware
you control.

Built for **one machine serving one lab**: experiments run on **port 80**, this app runs
on **port 8000**. An LXC container, a VM, a cloud VPS or a bare-metal box all work, and
all work the same way — if it runs Debian or Ubuntu with systemd, the steps below are
identical.

```
  ┌─ port 8000 ──────────────────┐        ┌─ port 80 ───────────────┐
  │  otreedeploy                 │        │                         │
  │                              │        │   ultimatum_2026        │
  │   ultimatum_2026  ● port 80  │───────▶│   (the live experiment) │
  │   trust_game      ○ stopped  │        │                         │
  │   public_goods    ○ stopped  │        └─────────────────────────┘
  │                              │
  │   [ Upload an experiment ]   │
  └──────────────────────────────┘
```

## What it does

- **Upload and deploy.** Drop in the `.otreezip` from `otree zip`. It is checked, its
  dependencies installed into their own virtualenv with `uv`, and the experiment
  test-started on a private port — all before anything touches port 80.
- **One live experiment, switched with a click.** Many projects can be uploaded; exactly
  one serves participants. A reboot brings the same one back.
- **Never silently loses data.** Re-uploading replaces code and keeps the database.
  Anything destructive takes a `pg_dump` backup first, and refuses outright if
  participants are mid-session.
- **Rollback.** Every deploy is a release; the last five stay on disk. If a restart fails,
  it rolls back on its own and the previous version keeps serving.
- **Data downloads for any experiment, running or not.** Stopped projects are started
  privately for a moment to produce the CSV, without disturbing whatever is live.
- **Live logs**, streamed from the journal into the browser.
- **One password on the management app**, so a participant who finds port 8000 cannot
  stop the study they are sitting in. The experiment on port 80 stays open, as it must.
- **English and Simplified Chinese**, switchable from any page.

## Setting up the server

You need one machine running **Debian or Ubuntu** with **systemd**, and root on it.
That is the whole requirement. Experiments are supervised as systemd units and their
logs are read from the journal, and `bootstrap.sh` installs packages with `apt-get`;
nothing else ties this to a particular kind of machine.

So all of these are set up identically — same commands, same result:

| Where | Worth knowing |
|---|---|
| **LXC container** — Proxmox, Incus | What this was written for. An **unprivileged** container (the Proxmox GUI default) is fine. Give it a static IP, and a root password or SSH key so you can log in. |
| **A VM** — Proxmox, VMware, VirtualBox, KVM | Nothing special at all. |
| **Bare metal** | Also nothing special, but check that nothing else on the box already wants port 80 or PostgreSQL — likelier on a machine that has other jobs. |
| **A cloud VPS** — Hetzner, DigitalOcean, Linode, EC2… | The sensible choice when participants are online rather than in a room. Read [If it faces the internet](#if-it-faces-the-internet) first: the defaults here assume a private network. |

> **Not a Docker or Podman application container.** Those have no init system, so there
> is no `systemctl` to supervise experiments with and no journal to read their logs from.
> A *system* container such as LXC or Incus runs systemd normally and is fine; an app
> container is a different thing and this will not work in one.

Give it enough disk for your data: each experiment carries its own virtualenv, so budget
roughly 500 MB per project plus whatever the data comes to.

Log in as root — `ssh root@<host>` — and get the code onto the machine, whichever way
suits you. `bootstrap.sh` copies itself into `/opt/otreedeploy`, so it does not matter
where you unpack it or what the directory is called.

**Download the tarball** — nothing to install first, and the same steps work for a
release you built yourself:

```bash
apt update && apt install -y curl ca-certificates

curl -L https://github.com/mjiangsjtu/otreedeploy/archive/refs/heads/main.tar.gz | tar xz
cd otreedeploy-main
./bootstrap.sh
```

**Or clone it**, if you want `git pull` to be how you upgrade:

```bash
apt update && apt install -y git

git clone https://github.com/mjiangsjtu/otreedeploy.git /opt/otreedeploy
cd /opt/otreedeploy
./bootstrap.sh
```

**Or copy a tarball straight in**, with no GitHub involved — the route to use when you
are testing a local change, or when you would rather not publish the code at all:

```bash
# on your own machine, one directory above the working copy
tar czf otreedeploy.tar.gz \
    --exclude=.venv --exclude=__pycache__ --exclude=.pytest_cache otreedeploy
scp otreedeploy.tar.gz root@<host>:/root/

# then on the server
tar xzf /root/otreedeploy.tar.gz -C /root
cd /root/otreedeploy
./bootstrap.sh
```

> This last route avoids GitHub, not the internet. `bootstrap.sh` still needs to reach
> the network for `apt`, for `uv`, and for the PyPI packages each experiment depends on.

`bootstrap.sh` runs **as root** — that is where a fresh container or VPS starts you, and
exactly the privilege provisioning needs. It installs PostgreSQL and `uv`, creates the service users,
sets up the databases, installs the systemd units, and starts the app. It is idempotent,
so re-running it is also how you upgrade: unpack the new version anywhere and run it
again. Your experiments, their databases and your password all survive.

**It asks you to choose a password** for the management page. A re-run keeps the one you
already set rather than asking again. To install without a prompt — from a provisioning
script, say:

```bash
OTREEDEPLOY_PASSWORD='something long' ./bootstrap.sh
```

When it finishes it prints the two URLs. Open the management one, sign in, and upload an
`.otreezip`.

### Ports

| Port | Serves | Who should reach it |
|---|---|---|
| 80 | The live experiment | Participants — lab machines, or wider if you run online |
| 8000 | This management app | You and your experimenters, on the lab network only |

**The management app asks for a password**, chosen when you run `bootstrap.sh`. Port 80
does not and must not — participants have to reach the experiment without signing in.
The password is what stops somebody at a lab machine from finding port 8000 and stopping
the study they are sitting in, or downloading everyone else's data.

Change it later on the *System* page, or on the server:

```bash
sudo -u otreedeploy /opt/otreedeploy/.venv/bin/otreedeploy-passwd
```

A forgotten password is recovered the same way — that command overwrites whatever is
there, and needs no old password, because reaching a root shell on the server is
already more access than the password protects.

One password is not a substitute for a firewall. On a lab network, open port 80 and
restrict port 8000 to your own subnet:

```bash
ufw allow 80/tcp
ufw allow from 192.0.2.0/24 to any port 8000 proto tcp   # your lab subnet
ufw enable
```

### If it faces the internet

Everything above assumes a private network. On a cloud VPS the same install is reachable
by everyone, and three things are worth knowing before you send participants to it.

**There is no TLS.** This app speaks plain HTTP and has no certificate handling at all.
Across the internet that means the management password, oTree's own admin password, and
every answer a participant submits travel in the clear. On a lab LAN that is a judgement
call; on the public internet it is not really one.

**Keep port 8000 off the internet.** This is the cheapest of the three and needs no extra
software: do not open the port, and reach the management page through an SSH tunnel.

```bash
ssh -L 8000:127.0.0.1:8000 root@<host>
# then browse to http://127.0.0.1:8000
```

If colleagues need it too, restrict it to known addresses rather than opening it up —
the same `ufw` rule as above, with their address in place of the lab subnet.

**Port 80 has to be open**, because participants cannot sign in to anything. Putting TLS
in front of it means running a reverse proxy (Caddy is the least work — it obtains
certificates by itself) and moving the experiment off port 80 so the proxy can have it.
`OTREEDEPLOY_OTREE_PORT` does that; see [Configuration](#configuration), including the
step people miss when they change it on a server that already has projects on it.

That is a direction, not a tested recipe: `bootstrap.sh` neither installs nor
configures a proxy, and nothing in the test suite runs the app behind one.

## Using it

**Deploy an experiment.** In oTree Studio, download the `.otreezip`. Coding by hand
instead? Run `otree zip` in your project folder. Then drag the file onto the upload page.
The build log streams live; if anything fails, it says why and nothing changes for
participants.

**Go live.** Press *Make this live on port 80*. Participants then use
`http://<your-server>/`. The oTree admin is at the same address — username `admin`, with
the password shown on the project page.

> Two different passwords, easily confused. The one from `bootstrap.sh` guards **this
> app** on port 8000. The one on the project page is **oTree's own** admin password for
> that experiment, on port 80; each project has its own, and you can change it in the
> project's settings.

**Update a running study.** Upload a new version from the project page. Your collected
data is kept; only the code is replaced. If the new code fails to start, the old version
keeps serving.

> oTree has no database migrations. If you add or remove model fields, the existing
> database no longer matches your code and the experiment will not start. The deploy
> catches this before it goes live and points you at *Reset database*, which takes a
> backup first.

**Get your data.** The *Data & downloads* page offers the same CSV files as oTree's own
data page, per session or across all of them — and it works whether or not the experiment
is the live one.

**Protecting a session in progress.** If participants have been active in the last two
minutes, anything that would interrupt them is blocked with a count of who is affected.
Each blocked action offers an explicit override for when you really do mean it.

## Configuration

Five environment variables. Three are read by the running app, two only by
`bootstrap.sh` while it installs. There is no config file, and for an ordinary install
you need none of them.

### Read by the app

| Variable | Default | What it does |
|---|---|---|
| `OTREEDEPLOY_OTREE_PORT` | `80` | The port the live experiment serves on. Move it if something else needs 80 — a TLS reverse proxy, most likely. |
| `OTREEDEPLOY_ROOT` | `/` | Prefix for every path the app touches (`/srv/otree`, `/var/lib/otreedeploy`). Mainly a testing seam: it lets the whole app run against a throwaway directory. |
| `OTREEDEPLOY_DRY_RUN` | unset | Development only. SQLite instead of PostgreSQL, systemd simulated. Never set this on a real server. |

Set one by adding a drop-in to the management app's unit:

```bash
systemctl edit otreedeploy.service
# [Service]
# Environment=OTREEDEPLOY_OTREE_PORT=8080

systemctl restart otreedeploy      # `systemctl edit` reloads the daemon for you
```

> **Changing the experiment port needs one more step if projects already exist.**
> The app does not pass the port to each experiment directly — it writes `PORT=` into
> `/srv/otree/<slug>/env`, and the experiment's unit reads it from there. That file is
> rewritten when a project is **deployed** or when you press **Save settings**, and at no
> other time.
>
> So after restarting the app, the pages immediately show the new port while an existing
> experiment carries on serving the old one: the link says `:8080`, the connection is
> refused, and the System page reports *Responding to requests: no*. Press **Save
> settings** on each project (it re-renders the file and restarts the experiment), or
> re-upload it. Setting the variable before you deploy anything avoids all of this.

A value that is not a number, or is outside 1–65535, is rejected when a page renders
rather than at startup — so the app will start and then return 500s.

### Read by `bootstrap.sh` only

| Variable | Default | What it does |
|---|---|---|
| `OTREEDEPLOY_PORT` | `8000` | The management app's own port. `bootstrap.sh` writes it into the systemd unit at install time. |
| `OTREEDEPLOY_PASSWORD` | — | Sets the management password without prompting, for unattended installs. Ignored if a password is already set. |

```bash
OTREEDEPLOY_PORT=9000 OTREEDEPLOY_PASSWORD='something long' ./bootstrap.sh
```

The running app never reads either one, so setting them in a systemd drop-in does
nothing — they belong on the `bootstrap.sh` command line.

### Per experiment, in the web UI

The project page carries the settings that differ from one study to the next: its
display name, **oTree's own admin password** (not the one guarding this app), and the
access level, `STUDY` or `DEMO`. The last two are handed to the experiment as
`OTREE_ADMIN_PASSWORD` and `OTREE_AUTH_LEVEL`; what the levels actually do is oTree's
business, and [its documentation](https://otree.readthedocs.io/) defines them.

Saving re-renders the environment file, and restarts the experiment if it is running —
otherwise the change waits until it next starts.

### In the source

The remaining knobs are module constants in `otreedeploy/config.py` rather than
environment variables, on the grounds that a single lab rarely needs to change them.
Edit and restart `otreedeploy.service` if you do:

| Constant | Default | |
|---|---|---|
| `MAX_UPLOAD_BYTES` | 512 MB | Largest `.otreezip` accepted |
| `KEEP_RELEASES` | 5 | Releases kept on disk per project |
| `PARTICIPANT_ACTIVE_SECONDS` | 120 | How recently someone must have loaded a page to count as mid-session, and so block a disruptive action |
| `BOOT_TIMEOUT_SECONDS` | 90 | How long a deploy waits for oTree to answer before calling it failed |
| `EPHEMERAL_IDLE_SECONDS` | 120 | How long a stopped project stays booted after a data download, so several exports reuse one start |

## How it is put together

```
/opt/otreedeploy/                 this app
/var/lib/otreedeploy/
    auth.json                     the management password, hashed (mode 0600)
    state.db                      deployment metadata (SQLite)
    uploads/<slug>/               every uploaded .otreezip, kept verbatim
    backups/<slug>/               pg_dump snapshots
/srv/otree/<slug>/
    env                           the systemd EnvironmentFile
    releases/<timestamp>/         extracted code + its own .venv
    current -> releases/…         flipped atomically on deploy
```

Each project gets its own PostgreSQL database (`otree_<slug>`), owned by its own role,
and its own virtualenv, so studies pinned to different oTree versions coexist, and no
project's code can read or reset another's data.

systemd supervises the experiments through a template unit, `otree@<slug>.service`.
Exactly one instance is *enabled* at a time — that is what makes a project the live one,
and what restores it after a reboot.

### Privileges

Nothing serves HTTP as root, and the management app never runs uploaded code.

- **`otree_<slug>`**: one system user and one PostgreSQL role per project, created
  when the project is first uploaded. An uploaded project is arbitrary Python. Installing
  it runs build hooks from PyPI, `resetdb` imports its models, and every boot runs
  experimenter code. So *every* phase runs as the project's own user under the same
  hardened systemd settings: the build, `resetdb`, the smoke test, the private boot for
  a data export, and the live service.
  - That user owns the project's database and its release directories, and nothing else.
  - It cannot read another project's code, environment file (admin password, REST key)
    or database. Other roles are refused `CONNECT` to its database.
  - It cannot read this app's files: the password hash, `state.db`, uploads, backups.
  - Port 80 comes from `CAP_NET_BIND_SERVICE`, not from root.
  - Names longer than Linux allows are shortened as `otree_<first 17>_<hash>`.
- **`otreedeploy`** runs this app. It accepts file uploads over the network, so it is
  unprivileged too.
  - Its entire root surface is one wrapper, `/usr/local/sbin/otree-ctl`, which validates
    every argument (project name, release name, port range) before acting. The sudoers
    rule grants that single command and contains no wildcards.
  - The app reaches each project's database through membership in that project's role,
    using peer authentication over the unix socket, so there are no passwords. It cannot
    create databases or roles itself.
  - The management password is hashed with PBKDF2 into a 0600 file owned by this user.
    It is deliberately kept out of `state.db`, so a copy of the metadata carries no
    credential.

What this does **not** cover: everyone who knows the management password can still see
and manage every project. Separate accounts with per-project access are a separate
layer, described under the management password.

**Upgrading from a version before per-project users:** re-running `bootstrap.sh` moves
each project to its own user and role. It first dumps every database to
`backups/<slug>/<time>-before-migration.sql.gz`, then restarts the live experiment
under its new user. Do it between sessions. The old shared `otree` user is left in
place but owns nothing.

## Running it on your laptop

```bash
uv venv && uv pip install -e ".[dev]"
OTREEDEPLOY_ROOT=/tmp/otreedeploy-dev OTREEDEPLOY_DRY_RUN=1 .venv/bin/otreedeploy --port 8123
```

Dry-run mode is not a set of stubs: experiments use SQLite instead of PostgreSQL and
systemd is simulated, but archives are really extracted, virtualenvs really built, and
oTree really started. You can upload a project and click through the whole app.

There is no password until you set one, which is what keeps this friction-free. To try
the login, point `otreedeploy-passwd` at the same root:

```bash
OTREEDEPLOY_ROOT=/tmp/otreedeploy-dev .venv/bin/otreedeploy-passwd
```

```bash
.venv/bin/python -m pytest              # everything
.venv/bin/python -m pytest -m "not slow"  # skip the real builds and boots
```

The slow tests genuinely deploy `tests/fixtures/labdemo.otreezip` — a real archive from
`otree zip` — build its venv, start oTree, create a session through oTree's REST API and
download the resulting CSV.

## Troubleshooting

| Symptom | Where to look |
|---|---|
| A deploy failed | The build log on the job page says which step and why |
| The experiment will not start | *Live log* on the project page (`journalctl -u otree@<slug>`) |
| The app itself is down | `systemctl status otreedeploy`, `journalctl -u otreedeploy -n 50` |
| Port 80 refused after a reboot | `systemctl is-enabled otree@<slug>` — activating a project is what enables it |
| Cannot bind port 80 | Rare, and LXC-specific; `sysctl net.ipv4.ip_unprivileged_port_start=80` in the container. VMs and VPSes are unaffected |
| `./bootstrap.sh: Permission denied` | The executable bit was lost (a ZIP download does this; the `.tar.gz` does not). Run `bash bootstrap.sh` |
| Forgotten the management password | `sudo -u otreedeploy /opt/otreedeploy/.venv/bin/otreedeploy-passwd` — it overwrites without asking for the old one |
| "Too many attempts" | Five wrong guesses lock that address out, for longer each time. Wait, or set a new password with the command above |
| Every page returns 503 | `/var/lib/otreedeploy/auth.json` is damaged. That is refused rather than treated as "no password"; rewrite it with the command above |

Backups are plain gzipped `pg_dump` files in `/var/lib/otreedeploy/backups/<slug>/`, and
also downloadable from the project page. To restore one:

```bash
gunzip -c backup.sql.gz | sudo -u otreedeploy psql otree_<slug>
```

## License

MIT — see [LICENSE](LICENSE). Copyright (c) 2026 mjiangsjtu.

Use it, change it, run it in your own lab. If it saves you a weekend, that is the point.
