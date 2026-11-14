"""Who may reach which page: one table, enforced before any route runs.

Every route has an entry in ``RULES``. A route without one is refused outright, and a
test walks every route of the app to make sure none is missing -- so a page added
later is closed until somebody decides who may see it, rather than open until
somebody remembers to protect it.

The rules:

- ``PUBLIC``: anyone, signed in or not (the sign-in page, the language switch, the
  stylesheet).
- ``SIGNED_IN``: any account. Some of these routes narrow it further themselves,
  because the project they concern is not in the path (an upload names it in its form,
  a deploy log belongs to the job's project); each such route says so where it does.
- ``MEMBER``: members of the project named by ``{slug}`` in the path, and admins.
  Anyone else gets "not found", so the names of other groups' projects do not leak.
- ``ADMIN``: admins only.

The experiments themselves, on port 80, are not behind any of this: participants must
reach them without signing in.
"""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import PlainTextResponse, RedirectResponse
from starlette.routing import Match

from . import accounts, config

COOKIE = "otreedeploy_session"

PUBLIC = "public"
SIGNED_IN = "signed_in"
MEMBER = "member"
ADMIN = "admin"

RULES: dict[tuple[str, str], str] = {
    ("GET", "/static"): PUBLIC,
    ("GET", "/lang/{code}"): PUBLIC,
    ("GET", "/login"): PUBLIC,
    ("POST", "/login"): PUBLIC,
    ("POST", "/logout"): PUBLIC,
    ("GET", "/"): SIGNED_IN,  # lists only the user's own projects
    ("GET", "/account"): SIGNED_IN,
    ("POST", "/account/password"): SIGNED_IN,
    ("GET", "/upload"): SIGNED_IN,  # route checks membership of ?slug=
    ("POST", "/upload"): SIGNED_IN,  # route checks membership of the form's slug
    ("GET", "/jobs/{job_id}"): SIGNED_IN,  # route checks the job's project
    ("GET", "/jobs/{job_id}/stream"): SIGNED_IN,  # route checks the job's project
    ("GET", "/p/{slug}"): MEMBER,
    ("POST", "/p/{slug}/activate"): MEMBER,  # forcing past the live project: route checks
    ("POST", "/p/{slug}/stop"): MEMBER,
    ("POST", "/p/{slug}/restart"): MEMBER,
    ("POST", "/p/{slug}/rollback"): MEMBER,
    ("POST", "/p/{slug}/settings"): MEMBER,
    ("POST", "/p/{slug}/reset-db"): MEMBER,
    ("POST", "/p/{slug}/restore"): MEMBER,
    ("POST", "/p/{slug}/delete"): MEMBER,
    ("POST", "/p/{slug}/members/add"): MEMBER,
    ("POST", "/p/{slug}/members/remove"): MEMBER,
    ("GET", "/p/{slug}/logs"): MEMBER,
    ("GET", "/p/{slug}/logs/stream"): MEMBER,
    ("GET", "/p/{slug}/data"): MEMBER,
    ("GET", "/p/{slug}/data/{kind}.csv"): MEMBER,
    ("GET", "/p/{slug}/backups/{name}"): MEMBER,
    ("GET", "/users"): ADMIN,
    ("POST", "/users/add"): ADMIN,
    ("POST", "/users/{name}/password"): ADMIN,
    ("POST", "/users/{name}/admin"): ADMIN,
    ("POST", "/users/{name}/disable"): ADMIN,
    ("GET", "/system"): ADMIN,
    ("POST", "/system/retention"): ADMIN,
    ("POST", "/system/deleted/remove"): ADMIN,
}

LOCKED_MESSAGE = (
    "No account exists yet, so this page is locked. Create the first one on the server:\n\n"
    "    sudo -u otreedeploy /opt/otreedeploy/.venv/bin/otreedeploy-users add admin --admin\n"
)


def current_user(request: Request) -> accounts.User | None:
    """The signed-in user, as resolved by ``EnforceAccess`` for this request."""
    return request.scope.get("state", {}).get("user")


def _resolve(scope) -> tuple[str | None, str | None, dict]:
    """(route path, rule, path params) for the route this request will reach."""
    method = "GET" if scope["method"] == "HEAD" else scope["method"]
    for route in scope["app"].router.routes:
        match, child = route.matches(scope)
        if match == Match.NONE:
            continue
        path = getattr(route, "path", None)
        params = child.get("path_params", {}) if match == Match.FULL else {}
        return path, RULES.get((method, path)), params
    return None, None, {}


class EnforceAccess:
    """Plain ASGI, like the rest of the middleware: two pages stream server-sent events
    for as long as the tab is open, which the request/response base class mishandles."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope)

        try:
            accounts.init()
            if accounts.has_users():
                user = accounts.session_user(request.cookies.get(COOKIE))
            elif config.dry_run():
                # A laptop with no accounts set up: open, as it always was.
                user = accounts.DEV_USER
            else:
                # A real server with no account must not serve the app unprotected.
                await PlainTextResponse(LOCKED_MESSAGE, status_code=503)(scope, receive, send)
                return
        except accounts.AccountError as exc:
            await PlainTextResponse(str(exc), status_code=503)(scope, receive, send)
            return
        scope.setdefault("state", {})["user"] = user
        from . import db

        db.actor.set(user.name if user else None)

        path, rule, params = _resolve(scope)
        if path is not None and rule is None:
            await PlainTextResponse(
                "This page has no access rule, so it is closed (otreedeploy.access.RULES).",
                status_code=500,
            )(scope, receive, send)
            return
        if rule == PUBLIC:
            await self.app(scope, receive, send)
            return
        if user is None:
            await self._sign_in(request)(scope, receive, send)
            return
        if rule == ADMIN and not user.is_admin:
            await PlainTextResponse("Only an admin can use this page.", status_code=403)(
                scope, receive, send)
            return
        if rule == MEMBER and not accounts.can_access(user, params.get("slug")):
            await PlainTextResponse("There is no such project.", status_code=404)(
                scope, receive, send)
            return
        await self.app(scope, receive, send)

    @staticmethod
    def _sign_in(request: Request):
        # A page the browser navigates to should show the sign-in form. A background
        # request -- the log streams above all -- needs a status its JavaScript can act
        # on, not a sign-in page delivered as though it were the data.
        if "text/event-stream" in request.headers.get("accept", ""):
            return PlainTextResponse("not signed in", status_code=401)
        target = request.url.path
        if request.url.query:
            target += f"?{request.url.query}"
        from urllib.parse import quote

        return RedirectResponse(f"/login?next={quote(target)}", status_code=303)
