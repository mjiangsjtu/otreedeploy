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
- **Accounts for each experimenter.** Each person sees only the experiments they are a
  member of; admins see all and manage accounts. Every data download and every action
  is recorded in an audit trail kept in the system journal. The experiment on port 80
  stays open, as it must.
- **Retention**: how long backups and uploaded files are kept, and a delete that
  really deletes.
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
again. Your experiments, their databases and your accounts all survive.

**It asks you to choose a password** for the first account, the admin `admin`. A re-run
keeps the accounts rather than asking again; an install from before accounts existed
keeps its management password, which becomes `admin`'s. To install without a prompt —
from a provisioning script, say:

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

**The management app asks you to sign in.** Port 80 does not and must not —
participants have to reach the experiment without signing in. Accounts are what stop
somebody at a lab machine from finding port 8000 and stopping the study they are
sitting in, and what keep each group's data to that group:

- **Users** (admins only): create an account for each experimenter. A one-time password
  is shown once; they change it under their name, top right.
- **Projects**: whoever uploads a project is its member; members add colleagues under
  *People with access* on the project page. Nobody else sees that the project exists.
  Admins see everything.
- **Audit trail** (System page): who signed in, who downloaded which data, who
  deployed, reset, restored or deleted what, and when. It is kept in the system journal,
  which neither this app nor the experiments can alter:
  `journalctl -t otreedeploy-audit _UID=$(id -u otreedeploy)`. How long the journal
  keeps it is set in `/etc/systemd/journald.conf` (`MaxRetentionSec`).
- **Retention** (System page): how many days backups and uploaded files are kept.
  Nothing is deleted until you set it. *Delete* on a project can also delete every
  backup and upload of it at once, for a request to erase the data.

From a shell on the server, for setting up or for recovery (no old password needed —
a root shell is already more access than the accounts protect):

```bash
sudo -u otreedeploy /opt/otreedeploy/.venv/bin/otreedeploy-users list
sudo -u otreedeploy /opt/otreedeploy/.venv/bin/otreedeploy-users passwd admin
sudo -u otreedeploy /opt/otreedeploy/.venv/bin/otreedeploy-users add maria
```

Accounts are not a substitute for a firewall. On a lab network, open port 80 and
restrict port 8000 to your own subnet:

```bash
ufw allow 80/tcp
ufw allow from 192.0.2.0/24 to any port 8000 proto tcp   # your lab subnet
ufw enable
```

### If it faces the internet: HTTPS

Everything above assumes a **closed lab network**, where plain HTTP is the usual practice
for oTree: nothing outside the lab can see the traffic. Across the internet that is not
acceptable — the management password, oTree's admin password and every answer a
participant submits would travel unencrypted. The app warns you (on install, and on
every page for admins) when the server has a public internet address and no HTTPS.

**With a domain name** that points at the server, one re-run gives it HTTPS:

```bash
OTREEDEPLOY_DOMAIN=lab.example.edu ./bootstrap.sh
```

This installs [Caddy](https://caddyserver.com) from Ubuntu's own packages and puts it in
front of everything:

| Address | Serves |
|---|---|
| `https://lab.example.edu/` | the live experiment, for participants |
| `https://lab.example.edu:8443/` | this management app |
| `http://lab.example.edu/` | a redirect to HTTPS |

The experiment and this app then listen on 127.0.0.1 only, so neither can be reached
around Caddy. Caddy obtains and renews the certificate from Let's Encrypt by itself.
Session cookies are marked Secure, and client addresses come from the proxy, for the
sign-in lockout and the audit trail. Re-running `bootstrap.sh` later (to upgrade) keeps
HTTPS on; `OTREEDEPLOY_DOMAIN=none` turns it off again. Switching restarts the live
experiment, so do it between sessions.

For Let's Encrypt to issue the certificate, the name must resolve to this server and
ports **80 and 443 must be reachable from the internet**. Open 8443 only to the people
who manage experiments.

**Without a public certificate:** if your institution's IT installs a root certificate
on the lab machines, `OTREEDEPLOY_TLS=internal` makes Caddy use its own certificate
authority instead (its root certificate is in
`/var/lib/caddy/.local/share/caddy/pki/authorities/local/root.crt`). Without that
installation every browser shows a security warning, which is no way to run a session.

> **Tested, with one exception.** The test suite runs this HTTPS setup end to end:
> switching a server with a live study to HTTPS, participants over HTTPS, sign-in with
> Secure cookies, the same-origin check, the plain-HTTP ports closed, and upgrades
> keeping it. It does so with Caddy's internal authority and a made-up name, because
> obtaining a real certificate needs a real domain and a server reachable from the
> internet. That last step is Caddy's standard automatic HTTPS, but it has not been
> exercised here.

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

> oTree has no database migrations, and on its own it does not notice when your models
> and the database disagree. The study starts normally, then every participant gets an
> error page. So each upload compares the new code with the existing database before
> anything goes live:
>
> - **A new field or a new app:** added to the database automatically. Rows collected
>   earlier simply have the new field empty.
> - **A removed field:** ignored; the data already collected for it is kept.
> - **A field whose type changes** (say, a number becomes text), or a new required
>   field without a default: the upload is refused and the live study is untouched. You
>   can keep the old definition, upload the change as a new project, or use
>   *Reset database*, which deletes the collected data after taking a backup.

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
| `OTREEDEPLOY_OTREE_PORT` | `80` | The port the live experiment serves on. HTTPS mode sets it to 8080 on loopback by itself; change it by hand only if something else needs port 80. |
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
| `OTREEDEPLOY_PASSWORD` | — | The password of the first account, `admin`, without prompting, for unattended installs. Ignored once any account exists. |

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
    accounts.db                   accounts, memberships, sessions (hashed; mode 0600)
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

With no accounts a dry run is open, which is what keeps it friction-free. To try
accounts, create one against the same root:

```bash
OTREEDEPLOY_ROOT=/tmp/otreedeploy-dev .venv/bin/otreedeploy-users add ada --admin
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
| Forgotten a password | An admin sets a new one on the Users page; for the admin, `sudo -u otreedeploy /opt/otreedeploy/.venv/bin/otreedeploy-users passwd admin` |
| "Too many attempts" | Five wrong guesses lock that name on that machine out, for longer each time. Wait, or set a new password as above |
| Every page returns 503 | No account exists yet, or an old `auth.json` could not be migrated. The page says which; create an account with `otreedeploy-users add admin --admin` |

Backups are plain gzipped `pg_dump` files in `/var/lib/otreedeploy/backups/<slug>/`, and
also downloadable from the project page. To put one back, use **Restore a backup** in the
project's danger zone. It works like this:

1. The current data is backed up first (`…-before-restore.sql.gz`), so a restore can be
   undone the same way.
2. The backup is loaded into a fresh database in a single transaction that stops at
   the first error. A backup that does not load cleanly leaves the data exactly as it was.
3. The restored database replaces the current one. The experiment is restarted if it
   was running.

The same from a shell, for example on a copy of the server:

```bash
sudo /usr/local/sbin/otree-ctl restore <slug> < /var/lib/otreedeploy/backups/<slug>/<backup>.sql.gz
```

The project must exist on the server: after deleting one, upload it again under the same
name, then restore.

## License

MIT — see [LICENSE](LICENSE). Copyright (c) 2026 mjiangsjtu.

Use it, change it, run it in your own lab. If it saves you a weekend, that is the point.
