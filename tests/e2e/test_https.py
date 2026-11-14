"""HTTPS through Caddy (OTREEDEPLOY_DOMAIN), switched on for a server already in use.

What this cannot test is obtaining a certificate from Let's Encrypt for a real domain:
that needs a public name pointing at the machine and ports 80/443 open to the internet.
So the certificate here comes from Caddy's own authority (OTREEDEPLOY_TLS=internal)
and the name is a made-up one resolved inside the container. Everything else is the
real thing: Caddy in front, the experiment and the app moved to loopback, cookies,
the same-origin check and client addresses through the proxy.
"""
from __future__ import annotations

import shlex

import httpx

from . import projects
from .server import PASSWORD, SRC_IN_CONTAINER, deploy_live, wait_for

DOMAIN = "lab.test"
CA = "/var/lib/caddy/.local/share/caddy/pki/authorities/local/root.crt"


def _curl(server, *args: str) -> str:
    """curl from inside the server, trusting Caddy's authority, resolving lab.test."""
    quoted = " ".join(shlex.quote(a) for a in args)
    return server.sh(f"curl -s --cacert {CA} {quoted}", check=False).out


def test_switching_a_running_server_to_https(server, manager, workdir):
    experiment, codes = deploy_live(server, manager, projects.labdemo(workdir), sessions=1)
    participant = experiment.participant_codes(codes[0])[0]
    server.sh(f"echo '127.0.0.1 {DOMAIN}' >> /etc/hosts")

    result = server.sh(f"cd {SRC_IN_CONTAINER} && ./bootstrap.sh", check=False,
                       env={"OTREEDEPLOY_DOMAIN": DOMAIN, "OTREEDEPLOY_TLS": "internal"})
    assert result.ok, result.out[-3000:]
    assert f"https://{DOMAIN}:8443" in result.out

    # Participants: the study is served over HTTPS, its data intact.
    wait_for(lambda: _curl(server, "-o", "/dev/null", "-w", "%{http_code}",
                           f"https://{DOMAIN}/") not in {"", "000", "502"},
             timeout=90, what="the experiment through Caddy")
    status = _curl(server, "-L", "-o", "/dev/null", "-w", "%{http_code}",
                   f"https://{DOMAIN}/InitializeParticipant/{participant}")
    assert status == "200"
    # Plain HTTP only redirects.
    assert _curl(server, "-o", "/dev/null", "-w", "%{http_code} %{redirect_url}",
                 f"http://{DOMAIN}/").startswith(("301 https", "308 https"))

    # Nothing but Caddy is reachable from the network.
    for port in (8000, 8080):
        try:
            httpx.get(f"http://{server.ip}:{port}/", timeout=5)
            reachable = True
        except httpx.HTTPError:
            reachable = False
        assert not reachable, f"port {port} answers from outside"

    # The management app: sign in over HTTPS; the cookie is Secure.
    headers = _curl(server, "-D", "-", "-o", "/dev/null", "-c", "/tmp/jar",
                    "-H", f"Origin: https://{DOMAIN}:8443",
                    "--data-urlencode", "username=admin",
                    "--data-urlencode", f"password={PASSWORD}",
                    f"https://{DOMAIN}:8443/login")
    assert headers.startswith("HTTP/") and " 303" in headers.splitlines()[0], headers
    cookie = next(line for line in headers.splitlines()
                  if line.lower().startswith("set-cookie: otreedeploy_session"))
    assert "secure" in cookie.lower()

    # A form from this app's own HTTPS origin is accepted; from the experiment's, refused.
    ok = _curl(server, "-b", "/tmp/jar", "-o", "/dev/null", "-w", "%{http_code}",
               "-H", f"Origin: https://{DOMAIN}:8443", "-X", "POST",
               f"https://{DOMAIN}:8443/p/labdemo/restart")
    assert ok == "303"
    forged = _curl(server, "-b", "/tmp/jar", "-o", "/dev/null", "-w", "%{http_code}",
                   "-H", f"Origin: https://{DOMAIN}", "-X", "POST",
                   f"https://{DOMAIN}:8443/p/labdemo/restart")
    assert forged == "403"

    # The project page shows participants the HTTPS address.
    page = _curl(server, "-b", "/tmp/jar", f"https://{DOMAIN}:8443/p/labdemo")
    assert f"https://{DOMAIN}/" in page

    # A plain re-run (an upgrade) keeps HTTPS without being told again.
    again = server.sh(f"cd {SRC_IN_CONTAINER} && ./bootstrap.sh", check=False)
    assert again.ok, again.out[-3000:]
    assert f"https://{DOMAIN}:8443" in again.out
    assert _curl(server, "-L", "-o", "/dev/null", "-w", "%{http_code}",
                 f"https://{DOMAIN}/InitializeParticipant/{participant}") == "200"
