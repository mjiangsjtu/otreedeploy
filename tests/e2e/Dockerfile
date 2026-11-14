# A disposable "fresh server" for the end-to-end suite: Ubuntu 24.04 with systemd as
# PID 1, so units, sudo, peer authentication and the journal are all the real thing.
#
# The packages bootstrap.sh would install are pre-installed here only to save time on
# every run; bootstrap.sh still runs `apt-get install` against them, still installs uv
# itself, and does everything else exactly as it would on a real machine.
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive \
    container=docker

RUN apt-get update -qq \
 && apt-get install -y -qq --no-install-recommends \
        systemd systemd-sysv dbus iproute2 procps \
        postgresql postgresql-client \
        python3 python3-venv \
        curl ca-certificates git sudo acl \
 && apt-get clean \
 && rm -rf /var/lib/apt/lists/*

# Units that make no sense inside a container and only slow boot down or fail noisily.
RUN systemctl mask \
        systemd-udevd.service systemd-udev-trigger.service systemd-udevd-kernel.socket \
        systemd-udevd-control.socket getty.target console-getty.service \
        systemd-logind.service systemd-remount-fs.service \
        sys-kernel-debug.mount sys-kernel-tracing.mount \
 && systemctl set-default multi-user.target

STOPSIGNAL SIGRTMIN+3
CMD ["/sbin/init"]
