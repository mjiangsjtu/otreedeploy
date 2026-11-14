"""Entry point: `otreedeploy` starts the management web app."""

from __future__ import annotations

import argparse

import uvicorn

from . import config


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="otreedeploy",
        description="Web app for deploying oTree experiments on this server.",
    )
    # Behind the HTTPS proxy, loopback only: Caddy is the one way in.
    parser.add_argument("--host", default="127.0.0.1" if config.behind_proxy() else "0.0.0.0")
    parser.add_argument("--port", type=int, default=config.DEFAULT_APP_PORT)
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args()

    uvicorn.run(
        "otreedeploy.app:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="info",
        # Only behind our own proxy are X-Forwarded-* headers believed, and only from
        # loopback: they set the client address (sign-in lockouts, audit trail) and
        # the scheme (Secure cookies, the same-origin check).
        proxy_headers=config.behind_proxy(),
        forwarded_allow_ips="127.0.0.1",
    )


if __name__ == "__main__":
    main()
