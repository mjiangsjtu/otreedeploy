"""Password protection for the management app.

One password for the whole app, chosen by ``bootstrap.sh``. This is deliberately not a
user-account system: the lab has one person who deploys and a network full of
participants, and the only job here is that somebody who finds port 8000 from a subject
machine cannot stop the experiment they are sitting in, or download everyone's data.

**The experiment itself, on port 80, is untouched.** Participants must reach it without
signing in, so nothing in this module is wired into it.

The credential lives in its own file rather than in the SQLite state database. A hash
does not belong beside deployment metadata: ``state.db`` gets copied around when
somebody debugs, and this way such a copy carries no credential.
"""

from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import hmac
import json
import os
import secrets
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from starlette.requests import Request
from starlette.responses import PlainTextResponse, RedirectResponse

from . import config

COOKIE = "otreedeploy_session"
SESSION_MAX_AGE = 60 * 60 * 24 * 30

# PBKDF2 rather than scrypt or argon2: it is in the standard library on every Python
# this runs on, and the iteration count is the only thing to keep current. The ~0.3s it
# costs is also the throttle on guessing -- see the rate limiter below, which handles
# the rest.
ALGORITHM = "pbkdf2_sha256"
ITERATIONS = 600_000
MIN_PASSWORD_LENGTH = 8


class AuthError(Exception):
    """The credential file exists but cannot be used."""


@dataclass(frozen=True)
class _Credentials:
    iterations: int
    salt: bytes
    digest: bytes
    # Signs session cookies. Regenerated whenever the password changes, which is what
    # makes a password change sign every other browser out.
    secret: bytes


def _path() -> Path:
    return config.get_settings().state_dir / "auth.json"


def is_configured() -> bool:
    """Whether a password has been set at all.

    With no file, the app serves unprotected -- which is how it behaved before this
    existed, and what keeps a dry run on a laptop friction-free. ``bootstrap.sh`` always
    sets one, and every page carries a warning banner until it is set.
    """
    return _path().exists()


def _load() -> _Credentials:
    path = _path()
    try:
        raw = json.loads(path.read_text())
        if raw["algorithm"] != ALGORITHM:
            raise ValueError(f"unknown algorithm {raw['algorithm']!r}")
        return _Credentials(
            iterations=int(raw["iterations"]),
            salt=bytes.fromhex(raw["salt"]),
            digest=bytes.fromhex(raw["hash"]),
            secret=bytes.fromhex(raw["secret"]),
        )
    except (OSError, ValueError, KeyError, TypeError) as exc:
        # Deliberately not "then there is no password": a damaged file must lock the app
        # down, never open it.
        raise AuthError(f"{path} is unreadable: {exc}") from exc


def _derive(password: str, salt: bytes, iterations: int) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)


def set_password(password: str) -> Path:
    """Write a new credential file, replacing any existing one."""
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(
            f"the password must be at least {MIN_PASSWORD_LENGTH} characters long"
        )
    salt = secrets.token_bytes(16)
    payload = {
        "algorithm": ALGORITHM,
        "iterations": ITERATIONS,
        "salt": salt.hex(),
        "hash": _derive(password, salt, ITERATIONS).hex(),
        "secret": secrets.token_bytes(32).hex(),
    }
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    # Created 0600 before anything is written to it, so the hash is never briefly
    # readable by other users on the box.
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as out:
        json.dump(payload, out, indent=2)
        out.write("\n")
    tmp.replace(path)
    return path


def verify_password(password: str) -> bool:
    if not is_configured():
        return False
    creds = _load()
    candidate = _derive(password, creds.salt, creds.iterations)
    return hmac.compare_digest(candidate, creds.digest)


# ---------------------------------------------------------------------- session token
#
# Stateless: the cookie carries its own expiry and a signature over it. There is no
# server-side session table to grow or to lose on restart, and rotating the secret --
# which a password change does -- invalidates every cookie at once.


def issue_token(now: float | None = None) -> str:
    creds = _load()
    expires = int(now if now is not None else time.time()) + SESSION_MAX_AGE
    return f"{expires}.{_sign(creds.secret, expires)}"


def _sign(secret: bytes, expires: int) -> str:
    mac = hmac.new(secret, f"v1:{expires}".encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(mac).decode().rstrip("=")


def check_token(token: str | None) -> bool:
    if not token or not is_configured():
        return False
    expires_text, _, signature = token.partition(".")
    if not signature:
        return False
    try:
        expires = int(expires_text)
    except ValueError:
        return False
    creds = _load()
    # Compare before checking the clock, so a wrong signature and an expired cookie take
    # the same time to reject.
    valid = hmac.compare_digest(signature, _sign(creds.secret, expires))
    return valid and expires > time.time()


# ------------------------------------------------------------------------ rate limits
#
# The threat is somebody in the room guessing, not a botnet: a lockout that grows with
# failures and forgets on success is enough, and costs nothing when nobody is attacking.

MAX_FAILURES = 5
LOCKOUT_SECONDS = 60
MAX_LOCKOUT_SECONDS = 900

_attempts: dict[str, tuple[int, float]] = {}
_attempts_lock = threading.Lock()


def locked_for(client: str) -> int:
    """Seconds until ``client`` may try again; 0 when it may try now."""
    with _attempts_lock:
        _, until = _attempts.get(client, (0, 0.0))
    return max(0, int(round(until - time.monotonic())))


def note_failure(client: str) -> None:
    with _attempts_lock:
        failures, _ = _attempts.get(client, (0, 0.0))
        failures += 1
        wait = 0.0
        if failures >= MAX_FAILURES:
            # Doubling and capped: five quick guesses cost a minute, a persistent
            # guesser ends up at a quarter of an hour per attempt.
            wait = min(LOCKOUT_SECONDS * 2 ** (failures - MAX_FAILURES), MAX_LOCKOUT_SECONDS)
        _attempts[client] = (failures, time.monotonic() + wait)


def note_success(client: str) -> None:
    with _attempts_lock:
        _attempts.pop(client, None)


def reset_rate_limits() -> None:
    """For tests, and for the password-change path, which is already authenticated."""
    with _attempts_lock:
        _attempts.clear()


# ------------------------------------------------------------------------- middleware

# Reachable signed out. `/lang/` is here so the login page can be read in Chinese by
# somebody who has not signed in yet; it only sets a cookie and redirects.
PUBLIC_PREFIXES = ("/static/", "/lang/")
PUBLIC_PATHS = frozenset({"/login", "/logout"})


def is_public(path: str) -> bool:
    return path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES)


class RequireLogin:
    """Turns away unauthenticated requests before they reach any route.

    Plain ASGI rather than ``BaseHTTPMiddleware`` on purpose: two pages stream server-
    sent events for as long as the tab is open, and the request/response middleware base
    class does not leave those alone.
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http" or not is_configured():
            await self.app(scope, receive, send)
            return

        request = Request(scope)
        try:
            # Checked before the public paths, so a damaged credential file takes the
            # whole app down visibly instead of leaving the login form as the one
            # reachable page, where it would reject every password without saying why.
            _load()
            allowed = is_public(request.url.path) or check_token(request.cookies.get(COOKIE))
        except AuthError as exc:
            await PlainTextResponse(str(exc), status_code=503)(scope, receive, send)
            return

        if allowed:
            await self.app(scope, receive, send)
            return
        await self._deny(request)(scope, receive, send)

    @staticmethod
    def _deny(request: Request):
        # A page the browser is navigating to should show the login form. A background
        # request -- the log streams, above all -- should get a status its JavaScript can
        # act on, not a login page delivered as though it were the data.
        if "text/event-stream" in request.headers.get("accept", ""):
            return PlainTextResponse("not signed in", status_code=401)
        target = request.url.path
        if request.url.query:
            target += f"?{request.url.query}"
        from urllib.parse import quote

        return RedirectResponse(f"/login?next={quote(target)}", status_code=303)


class SameOriginWrites:
    """Refuses any state-changing request that did not come from this app's own pages.

    Plain ASGI, like ``RequireLogin``. The session cookie is SameSite=Lax, but "site"
    ignores the port: the experiment on port 80 -- uploaded HTML, and anything a
    participant manages to inject into it -- is same-site with this app on port 8000, so
    a page there could submit this app's forms with the experimenter's cookie. The
    browser's Origin header does include the port, so a write is accepted only when
    Origin (or, failing that, Referer) names exactly this app's origin. A request that
    carries neither is refused too: every browser sends Origin on a form POST.
    """

    SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http" or scope["method"] in self.SAFE_METHODS:
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        if self.same_origin(request):
            await self.app(scope, receive, send)
            return
        await PlainTextResponse(
            "Refused: this request did not come from the otreedeploy page itself. "
            "If you clicked a button here, reload the page and try again.",
            status_code=403,
        )(scope, receive, send)

    @staticmethod
    def same_origin(request: Request) -> bool:
        from urllib.parse import urlsplit

        host = request.headers.get("host", "")
        expected = f"{request.url.scheme}://{host}".lower()
        origin = request.headers.get("origin")
        if origin and origin != "null":
            return origin.lower().rstrip("/") == expected
        referer = request.headers.get("referer")
        if referer:
            parts = urlsplit(referer)
            return f"{parts.scheme}://{parts.netloc}".lower() == expected
        return False


def safe_next(target: str | None) -> str:
    """Where to send someone after signing in, refusing anywhere off this app.

    "//host" is a protocol-relative URL: it starts with a slash and still leaves the
    site, which is exactly the shape an open-redirect uses.
    """
    if not target or not target.startswith("/") or target.startswith("//"):
        return "/"
    return target


# -------------------------------------------------------------------------------- CLI


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="otreedeploy-passwd",
        description="Set the password for the otreedeploy management page.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit 0 if a password is already set, 1 if not; change nothing",
    )
    parser.add_argument(
        "--stdin",
        action="store_true",
        help="read the password from standard input instead of prompting",
    )
    args = parser.parse_args()

    if args.check:
        raise SystemExit(0 if is_configured() else 1)

    if args.stdin:
        password = sys.stdin.readline().rstrip("\n")
    else:
        password = getpass.getpass("New password: ")
        if password != getpass.getpass("Repeat password: "):
            print("Those did not match.", file=sys.stderr)
            raise SystemExit(1)

    try:
        path = set_password(password)
    except (ValueError, OSError) as exc:
        print(f"Could not set the password: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
    print(f"Password set ({path}). Everyone signed in elsewhere has been signed out.")


if __name__ == "__main__":
    main()
