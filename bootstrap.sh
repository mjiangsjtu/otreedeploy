#!/bin/bash
#
# otreedeploy bootstrap -- turns a fresh Debian or Ubuntu machine into an oTree lab
# server. LXC container, VM, cloud VPS or bare metal -- anything with systemd and apt.
#
# Unpack this anywhere on the machine and run it -- from a release tarball, a git clone,
# or a directory you copied in with scp. It copies itself to /opt/otreedeploy, so the
# directory you run it from does not matter:
#
#   ssh root@<host>
#   curl -L https://github.com/mjiangsjtu/otreedeploy/archive/refs/heads/main.tar.gz | tar xz
#   cd otreedeploy-main && ./bootstrap.sh
#
# Run this AS ROOT. That is the point: a fresh container or VPS hands you a root shell,
# and creating users, units and databases is exactly what root is for. Nothing serves HTTP as root
# afterwards -- this script creates two unprivileged users and the services run as those.
#
# Safe to re-run: every step is idempotent, so this doubles as the upgrade path.

set -euo pipefail

APP_DIR=/opt/otreedeploy
STATE_DIR=/var/lib/otreedeploy
OTREE_HOME=/srv/otree
APP_PORT="${OTREEDEPLOY_PORT:-8000}"
# The uv release this script is tested against (tests/e2e, run in CI). Pinned because
# uv's command-line behaviour changes between releases -- an unpinned install once broke
# re-running this script -- and because the deploy pipeline calls uv on every upload.
UV_VERSION=0.13.8

APP_USER=otreedeploy

log()  { printf '\n\033[1;34m==>\033[0m \033[1m%s\033[0m\n' "$*"; }
info() { printf '    %s\n' "$*"; }
die()  { printf '\n\033[1;31mError:\033[0m %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "run this as root (you are $(whoami))"
[[ -f "$(dirname "$0")/deploy/otree@.service" ]] || die "run this from inside the otreedeploy directory"

SRC_DIR=$(cd "$(dirname "$0")" && pwd)

# --------------------------------------------------------------------------- packages

log "Installing system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq \
    postgresql postgresql-client \
    python3 python3-venv \
    curl ca-certificates git sudo acl
info "done"

# ------------------------------------------------------------------------------- uv

log "Installing uv $UV_VERSION"
# /usr/local/bin/uv is the one the services find first on their PATH, so that is the
# copy to pin; a uv installed elsewhere for other purposes is left alone.
if [[ "$(/usr/local/bin/uv --version 2>/dev/null | awk '{print $2}')" != "$UV_VERSION" ]]; then
    curl -LsSf "https://astral.sh/uv/$UV_VERSION/install.sh" \
        | env UV_INSTALL_DIR=/usr/local/bin UV_NO_MODIFY_PATH=1 sh >/dev/null
fi
[[ "$(/usr/local/bin/uv --version 2>/dev/null | awk '{print $2}')" == "$UV_VERSION" ]] \
    || die "could not install uv $UV_VERSION into /usr/local/bin"
info "$(/usr/local/bin/uv --version)"

# ----------------------------------------------------------------------------- users

log "Creating the service user"
# `otreedeploy` runs the web app that accepts uploads. Unprivileged. Experiments do not
# run as this user: each project gets its own user (otree_<slug>) when it is first
# uploaded -- see deploy/otree-ctl, "provision".
id -u "$APP_USER" >/dev/null 2>&1 || \
    useradd --system --home-dir "$APP_DIR" --shell /usr/sbin/nologin "$APP_USER"
# Lets the app read the journal for the live log view without any privilege.
usermod -aG systemd-journal "$APP_USER"
# Installs from before per-project users put the app in the shared `otree` group; it
# needs no project's group any more.
if id -nG "$APP_USER" | tr ' ' '\n' | grep -qx otree; then
    gpasswd -d "$APP_USER" otree >/dev/null
fi
info "$APP_USER (runs the web app)"

# ------------------------------------------------------------------------ directories

log "Creating the directory layout"
# The app owns these. Each project directory inside $OTREE_HOME belongs to that
# project's group and is closed to every other project.
install -d -o "$APP_USER" -g "$APP_USER" -m 0755 "$OTREE_HOME"
# Older installs made this setgid to the shared group; numeric chmod keeps that bit.
chgrp "$APP_USER" "$OTREE_HOME"; chmod 0755 "$OTREE_HOME"; chmod g-s "$OTREE_HOME"
install -d -o "$APP_USER" -g "$APP_USER"   -m 0750 "$STATE_DIR"
install -d -o "$APP_USER" -g "$APP_USER"   -m 0750 "$STATE_DIR/uploads"
install -d -o "$APP_USER" -g "$APP_USER"   -m 0750 "$STATE_DIR/backups"
info "$OTREE_HOME (experiments), $STATE_DIR (uploads, backups, state)"

# ------------------------------------------------------------------------- postgresql

log "Configuring PostgreSQL"
systemctl enable --now postgresql

# Roles use peer authentication over the unix socket, so there are no passwords to
# manage or leak. Each project's database is owned by that project's own role, created
# by `otree-ctl provision`; the app is granted membership in each, which is what lets it
# read session lists and run pg_dump. It cannot create databases or roles itself.
run_sql() { su - postgres -c "psql -tAc \"$1\""; }

if [[ -z "$(run_sql "SELECT 1 FROM pg_roles WHERE rolname='$APP_USER'")" ]]; then
    run_sql "CREATE ROLE $APP_USER LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE"
    info "created role $APP_USER"
fi
# Older installs let the app create databases; provisioning does that now.
run_sql "ALTER ROLE $APP_USER NOCREATEDB NOCREATEROLE" >/dev/null

# ---------------------------------------------------------------- the management app

log "Installing the management app"
if [[ "$SRC_DIR" != "$APP_DIR" ]]; then
    install -d -o "$APP_USER" -g "$APP_USER" -m 0755 "$APP_DIR"
    cp -a "$SRC_DIR/." "$APP_DIR/"
fi
chown -R "$APP_USER:$APP_USER" "$APP_DIR"

# Build the app's own venv as the app user, so nothing in it is root-owned. On a re-run
# the venv already exists and the running service is using it: keep it and reinstall
# into it, rather than deleting it under the service (which current uv also refuses to
# do without --clear -- the reason upgrades used to abort here).
if [[ -x "$APP_DIR/.venv/bin/python" ]]; then
    info "keeping the existing $APP_DIR/.venv"
else
    su -s /bin/bash "$APP_USER" -c "cd $APP_DIR && /usr/local/bin/uv venv --python 3.12 .venv" >/dev/null
fi
su -s /bin/bash "$APP_USER" -c "cd $APP_DIR && /usr/local/bin/uv pip install --python $APP_DIR/.venv/bin/python -e ." >/dev/null
info "installed the app into $APP_DIR/.venv"

# ------------------------------------------------------------------------- password

log "Setting the management password"
# Guards this app only. The experiments on port 80 stay open -- participants have to
# reach them -- so the point of the password is that a subject who finds port $APP_PORT
# cannot stop the study they are sitting in, or download everyone else's data.
PASSWD_BIN="$APP_DIR/.venv/bin/otreedeploy-passwd"
as_app() { su -s /bin/bash "$APP_USER" -c "$1"; }

PASSWORD_WAS_SET=no
if as_app "$PASSWD_BIN --check"; then
    PASSWORD_WAS_SET=yes
    info "already set -- keeping it (change it on the System page, or with $PASSWD_BIN)"
elif [[ -n "${OTREEDEPLOY_PASSWORD:-}" ]]; then
    # printf is a shell builtin and the value is piped, so it never appears in `ps`.
    printf '%s\n' "$OTREEDEPLOY_PASSWORD" | as_app "$PASSWD_BIN --stdin"
    info "taken from OTREEDEPLOY_PASSWORD"
elif [[ -t 0 ]]; then
    while true; do
        read -rsp "    Choose a password for the management page: " pw1; echo
        read -rsp "    Type it again: " pw2; echo
        if [[ "$pw1" != "$pw2" ]]; then
            info "those did not match -- try again"
        elif printf '%s\n' "$pw1" | as_app "$PASSWD_BIN --stdin" >/dev/null; then
            break
        fi
    done
    unset pw1 pw2
    info "password set"
else
    die "no password is set and there is no terminal to ask on.
    Re-run as:  OTREEDEPLOY_PASSWORD='something-long' ./bootstrap.sh"
fi

# ------------------------------------------------------------------------ privileges

log "Installing the privileged wrapper"
install -o root -g root -m 0755 "$SRC_DIR/deploy/otree-ctl" /usr/local/sbin/otree-ctl
install -o root -g root -m 0440 "$SRC_DIR/deploy/sudoers.d/otreedeploy" /etc/sudoers.d/otreedeploy
visudo -cf /etc/sudoers.d/otreedeploy >/dev/null || die "the sudoers rule is invalid"
info "/usr/local/sbin/otree-ctl -- the app's entire root surface"

# --------------------------------------------------------------------------- systemd

log "Installing systemd units"
install -o root -g root -m 0644 "$SRC_DIR/deploy/otree@.service" /etc/systemd/system/
install -o root -g root -m 0644 "$SRC_DIR/deploy/otreedeploy.service" /etc/systemd/system/
if [[ "$APP_PORT" != "8000" ]]; then
    sed -i "s/--port 8000/--port $APP_PORT/" /etc/systemd/system/otreedeploy.service
fi
systemctl daemon-reload

# -------------------------------------------------------- per-project identities

log "Giving each project its own user"
# Projects deployed before per-project users ran as one shared `otree` user that owned
# every database. Each moves to its own user and database role; its database is dumped
# first. Provisioning is idempotent, so on later runs this only re-checks permissions.
STAMP=$(date +%Y%m%d-%H%M%S)
projects=0
for dir in "$OTREE_HOME"/*/; do
    slug=$(basename "$dir")
    [[ $slug =~ ^[a-z][a-z0-9_]{0,38}$ && -d $dir/releases && ! -L ${dir%/} ]] || continue
    projects=$((projects + 1))
    migrating=no
    if [[ ! -f /etc/systemd/system/otree@$slug.service.d/10-identity.conf ]]; then
        migrating=yes
        if [[ -n "$(run_sql "SELECT 1 FROM pg_database WHERE datname='otree_$slug'")" ]]; then
            backup_dir="$STATE_DIR/backups/$slug"
            install -d -o "$APP_USER" -g "$APP_USER" -m 0750 "$backup_dir"
            dump="$backup_dir/$STAMP-before-migration.sql.gz"
            runuser -u postgres -- pg_dump --no-owner --no-privileges "otree_$slug" | gzip > "$dump"
            chown "$APP_USER:$APP_USER" "$dump"
            info "$slug: database backed up to $dump"
        fi
    fi
    user=$(/usr/local/sbin/otree-ctl provision "$slug")
    info "$slug runs as $user"
    if [[ $migrating == yes ]] && systemctl is-enabled --quiet "otree@$slug.service"; then
        systemctl restart "otree@$slug.service"
        info "$slug is live: restarted it as $user"
    fi
done
[[ $projects -gt 0 ]] || info "no projects yet"

# An install section changed between versions: re-enable the live experiment so its
# links match the unit file installed above (e.g. the postgresql.service link).
for unit in $(systemctl list-unit-files 'otree@*.service' --state=enabled --no-legend | awk '{print $1}'); do
    systemctl reenable "$unit" >/dev/null 2>&1 && info "re-enabled $unit"
done
systemctl enable otreedeploy.service
# `enable --now` would leave an already-running app on its old code, which makes this
# script useless as the upgrade path. Restart unconditionally instead -- it starts a
# stopped app and reloads a running one. Experiments are separate units and keep serving.
systemctl restart otreedeploy.service
info "otreedeploy.service enabled and (re)started"

# ----------------------------------------------------------------------------- checks

log "Checking the result"
sleep 2

failures=0
check() {
    if eval "$2" >/dev/null 2>&1; then
        printf '    \033[32m✓\033[0m %s\n' "$1"
    else
        printf '    \033[31m✗\033[0m %s\n' "$1"
        failures=$((failures + 1))
    fi
}

check "postgresql is running"           "systemctl is-active --quiet postgresql"
check "otreedeploy is running"          "systemctl is-active --quiet otreedeploy"
check "the app answers on port $APP_PORT" "curl -sf -o /dev/null http://127.0.0.1:$APP_PORT/"
check "the app can reach postgres"      "su -s /bin/bash $APP_USER -c 'psql -tAc \"SELECT 1\" postgres'"
check "the app can use otree-ctl"       "su -s /bin/bash $APP_USER -c 'sudo -n /usr/local/sbin/otree-ctl is-active nonexistent_probe; true'"
check "neither service runs as root"    "! systemctl show -p User otreedeploy | grep -q 'User=root'"
check "the management page asks for a password" \
    "curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:$APP_PORT/ | grep -q '^303$'"

if [[ $failures -gt 0 ]]; then
    die "$failures check(s) failed -- see 'journalctl -u otreedeploy -n 50'"
fi

ADDR=$(hostname -I 2>/dev/null | awk '{print $1}')
if [[ $PASSWORD_WAS_SET == yes ]]; then
    SIGN_IN="Sign in with the password you already had (this run kept it)"
else
    SIGN_IN="Sign in with the password you just chose"
fi
cat <<BANNER

  otreedeploy is ready.

    Manage experiments   http://${ADDR:-<this-server>}:$APP_PORT
    Experiments serve on http://${ADDR:-<this-server>}/     (port 80)

  $SIGN_IN, then upload an .otreezip to deploy
  your first experiment. Change the password later on the System page.

  The experiment on port 80 is deliberately open -- participants must reach it.
  The password only guards this management page. Keep port $APP_PORT reachable
  from your lab network only; see the README.

BANNER
