"""Readiness probing for an oTree instance.

``/favicon.ico`` is one of the few routes oTree serves without authentication, which
makes it the cheapest honest signal that the ASGI app is actually up rather than merely
that a port is open.
"""

from __future__ import annotations

import http.client
import socket
import time
import urllib.error
import urllib.request

from . import config


def port_open(port: int, host: str = "127.0.0.1", timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def responds(port: int, host: str = "127.0.0.1", timeout: float = 5.0) -> bool:
    """True if something speaks HTTP on this port without a server error.

    A redirect, a 403 or a 404 still proves the app is serving. A 5xx does not: oTree
    answers 500 when it cannot use its database, and "it starts and responds" must not
    be claimed for a server in that state. A connection failure or a non-HTTP response
    also means "not ready".

    ``HTTPException`` covers the garbage-on-the-wire cases -- a half-initialised server
    writing before it speaks HTTP, or a truncated reply. It also covers the TCP
    self-connect that a loopback probe hits when the port is not yet bound and the kernel
    hands the outgoing socket the same number: the probe reads its own request line back
    and ``BadStatusLine`` is raised. All of those mean "not ready", not "crash".
    """
    url = f"http://{host}:{port}{config.PROBE_PATH}"
    try:
        with urllib.request.urlopen(url, timeout=timeout):
            return True
    except urllib.error.HTTPError as exc:
        return exc.code < 500
    except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError):
        return False


def wait_for(predicate, timeout: float = config.BOOT_TIMEOUT_SECONDS) -> bool:
    """Poll ``predicate`` with backoff until it is true or the timeout expires."""
    deadline = time.monotonic() + timeout
    delay = 0.25
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(delay)
        delay = min(delay * 1.5, 2.0)
    return False


def wait_until_ready(
    port: int,
    host: str = "127.0.0.1",
    timeout: float = config.BOOT_TIMEOUT_SECONDS,
    should_continue=None,
) -> bool:
    """Poll until the instance answers, or the timeout expires.

    ``should_continue`` lets a caller abort early -- used when the process being waited
    for has already died, so we do not sit out the full timeout for nothing.
    """
    deadline = time.monotonic() + timeout
    delay = 0.25
    while time.monotonic() < deadline:
        if should_continue is not None and not should_continue():
            return False
        if responds(port, host=host, timeout=3.0):
            return True
        time.sleep(delay)
        delay = min(delay * 1.5, 2.0)
    return False


def free_port(start: int, end: int) -> int:
    """Find an unused loopback port in the range, for an ephemeral boot."""
    for port in range(start, end):
        if not port_open(port):
            with socket.socket() as probe:
                try:
                    probe.bind(("127.0.0.1", port))
                except OSError:
                    continue
            return port
    raise RuntimeError(f"no free port available in range {start}-{end}")
