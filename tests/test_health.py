"""Readiness probing, including the wire garbage a booting server can produce.

The probe is the gate every deploy passes through, so a probe that raises instead of
returning False takes the whole deploy down with it.
"""

from __future__ import annotations

import socket
import threading
from pathlib import Path

import pytest

from otreedeploy import otreeapi, config, health

KERNEL_PORT_RANGE = Path("/proc/sys/net/ipv4/ip_local_port_range")


@pytest.fixture
def fake_server():
    """Serve one connection with a canned byte string, then close."""
    servers = []

    def _serve(reply: bytes) -> int:
        listener = socket.socket()
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        servers.append(listener)

        def run() -> None:
            try:
                conn, _ = listener.accept()
            except OSError:
                return
            with conn:
                try:
                    conn.recv(4096)
                    if reply:
                        conn.sendall(reply)
                except OSError:
                    pass

        threading.Thread(target=run, daemon=True).start()
        return listener.getsockname()[1]

    yield _serve
    for listener in servers:
        listener.close()


def test_a_real_http_reply_is_ready(fake_server):
    port = fake_server(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")
    assert health.responds(port, timeout=2.0) is True


def test_an_error_status_still_counts_as_ready(fake_server):
    """A 403 proves the app is serving; only silence or garbage means not ready."""
    port = fake_server(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
    assert health.responds(port, timeout=2.0) is True


def test_a_server_error_is_not_ready(fake_server):
    """oTree answers 500 when it cannot use its database; that is not "it works"."""
    port = fake_server(b"HTTP/1.1 500 Internal Server Error\r\nContent-Length: 0\r\n\r\n")
    assert health.responds(port, timeout=2.0) is False


def test_a_server_that_is_not_this_project_is_not_taken_for_it(fake_server):
    """Something else on port 80 -- here, a plain web server -- must not count as the
    project having started."""
    port = fake_server(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n")
    assert otreeapi.serves_project(port, "key") is False


def test_non_http_reply_is_not_ready_rather_than_an_exception(fake_server):
    """The self-connect case: the probe reads back something that is not a status line.

    urllib raises http.client.BadStatusLine here, which is an HTTPException and not an
    OSError -- so it escapes a naive except clause and crashes the caller.
    """
    port = fake_server(b"GET /favicon.ico HTTP/1.1\r\n\r\n")
    assert health.responds(port, timeout=2.0) is False


def test_a_status_line_is_enough_even_if_the_body_is_cut_short(fake_server):
    """The probe only needs the headers -- a status line already proves an ASGI app
    answered. It never reads the body, so a truncated one is not a failure signal."""
    port = fake_server(b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\nshort")
    assert health.responds(port, timeout=2.0) is True


def test_connection_closed_without_a_reply_is_not_ready(fake_server):
    port = fake_server(b"")
    assert health.responds(port, timeout=2.0) is False


def test_nothing_listening_is_not_ready():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    assert health.responds(port, timeout=2.0) is False


def test_free_port_is_in_range_and_bindable():
    start, end = config.EPHEMERAL_PORT_RANGE
    port = health.free_port(start, end)
    assert start <= port < end
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", port))


@pytest.mark.skipif(
    not KERNEL_PORT_RANGE.exists(), reason="no /proc/sys/net/ipv4/ip_local_port_range"
)
def test_ephemeral_range_sits_below_the_kernel_ephemeral_range():
    """Overlapping it lets an outbound connection steal the port -- or the probe
    self-connect to a port oTree has not bound yet."""
    kernel_low = int(KERNEL_PORT_RANGE.read_text().split()[0])
    _, end = config.EPHEMERAL_PORT_RANGE
    assert end <= kernel_low, (
        f"EPHEMERAL_PORT_RANGE {config.EPHEMERAL_PORT_RANGE} overlaps the kernel's "
        f"ephemeral range starting at {kernel_low}"
    )


def test_unreachable_otree_degrades_instead_of_raising():
    """The REST helpers are written to return a default when oTree is unavailable.

    A transport failure has to arrive as OtreeError for that to work -- an instance can
    disappear between the readiness probe and the call.
    """
    from otreedeploy import otreeapi

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        dead_port = probe.getsockname()[1]

    assert otreeapi.otree_version(dead_port, "key") is None
    assert otreeapi.session_configs(dead_port, "key") == []
    assert otreeapi.apps_with_data(dead_port, "key") == []

    with pytest.raises(otreeapi.OtreeError):
        otreeapi.export_csv(dead_port, "key", "all_apps_wide")
