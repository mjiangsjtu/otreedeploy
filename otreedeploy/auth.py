"""Password hashing, sign-in rate limits, and the guard against forged writes.

Accounts, sessions and who may see what live in ``accounts`` and ``access``; this
module keeps the pieces they share with nothing else in particular.

**The experiment itself, on port 80, is untouched.** Participants must reach it without
signing in, so nothing here is wired into it.
"""

from __future__ import annotations

import hashlib
import threading
import time

from starlette.requests import Request
from starlette.responses import PlainTextResponse

# PBKDF2 rather than scrypt or argon2: it is in the standard library on every Python
# this runs on, and the iteration count is the only thing to keep current. The ~0.3s it
# costs is also the throttle on guessing -- see the rate limiter below, which handles
# the rest.
ALGORITHM = "pbkdf2_sha256"
ITERATIONS = 600_000
MIN_PASSWORD_LENGTH = 8


def _derive(password: str, salt: bytes, iterations: int) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)


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
    site, which is exactly the shape an open-redirect uses. Browsers also read a
    backslash as a slash, so "/\\host" is the same thing in disguise, and control
    characters can hide either.
    """
    if (not target or not target.startswith("/") or target.startswith("//")
            or "\\" in target or any(ord(c) < 32 for c in target)):
        return "/"
    return target
