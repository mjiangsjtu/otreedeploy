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
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=config.DEFAULT_APP_PORT)
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args()

    uvicorn.run(
        "otreedeploy.app:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="info",
    )


if __name__ == "__main__":
    main()
