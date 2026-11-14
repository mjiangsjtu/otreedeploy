"""The management web app.

Server-rendered Jinja, plain forms, and SSE for live logs. No build
step and no JavaScript toolchain on purpose: this has to still work in three years when
nobody remembers how it was built.

One password guards this app, set by bootstrap.sh; see ``auth``. The experiments
themselves are not guarded -- participants have to reach them.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import contextvars
import shutil
import tempfile
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Iterator
from urllib.parse import quote, urlencode

from fastapi import FastAPI, Form, Request, UploadFile
from fastapi.responses import (
    HTMLResponse,
    FileResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import (
    access,
    accounts,
    audit,
    auth,
    config,
    db,
    deploy,
    environment,
    health,
    i18n,
    otreeapi,
    postgres,
    projects,
    retention,
    sandbox,
    systemd,
)
from .archive import ArchiveError
from .deploy import DeployError, ParticipantsActive

BASE_DIR = Path(__file__).resolve().parent.parent


def _language(request) -> str:
    """Chosen language, or English. An explicit choice is stored in a cookie.

    The browser's Accept-Language is deliberately ignored: the default is English for
    everyone, and Chinese is something you switch to and stays switched.
    """
    return i18n.normalize(request.cookies.get(i18n.COOKIE))


def _i18n_context(request) -> dict:
    lang = _language(request)

    def t(text: str, **params) -> str:
        # The live port is wanted by enough strings to be worth supplying always;
        # str.format ignores a parameter the text does not mention.
        return i18n.translate(lang, text, port=config.otree_port(), **params)

    def msg(text: str) -> str:
        # For text that was not written in a template: notices, errors, event-log
        # entries. These may carry values from outside (file names, exception text).
        return i18n.message(lang, text, port=config.otree_port())

    return {
        "t": t,
        "msg": msg,
        "lang": lang,
        "html_lang": i18n.HTML_LANG.get(lang, lang),
        "languages": i18n.LANGUAGES,
        "ago": lambda value: i18n.ago(lang, value),
    }


def _auth_context(request) -> dict:
    """Who is signed in, for every page."""
    user = access.current_user(request)
    return {
        "user": user,
        "signed_in": user is not None and user is not accounts.DEV_USER,
        "is_admin": bool(user and user.is_admin),
        "dry_run": config.dry_run(),
    }


templates = Jinja2Templates(
    directory=str(BASE_DIR / "templates"),
    context_processors=[_i18n_context, _auth_context],
)


def _experiment_origin(request) -> str:
    """The address participants type. Port omitted when it is 80, as a browser would."""
    port = config.otree_port()
    host = request.url.hostname
    return f"http://{host}/" if port == 80 else f"http://{host}:{port}/"


# The live port is 80 in production but configurable elsewhere, so no template may
# hardcode it -- a page that names the wrong port sends people to a refused connection.
templates.env.globals["otree_port"] = config.otree_port
templates.env.globals["experiment_origin"] = _experiment_origin
templates.env.globals["min_password_length"] = auth.MIN_PASSWORD_LENGTH


def _user(request: Request) -> accounts.User:
    """The signed-in user. Routes only run once ``access.EnforceAccess`` found one."""
    user = access.current_user(request)
    assert user is not None
    return user


def _client(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _audit(request: Request, action: str, slug: str | None = None, detail: str = "") -> None:
    user = access.current_user(request)
    audit.record(action, user.name if user else None, slug, detail, client=_client(request))

@asynccontextmanager
async def _lifespan(_: FastAPI):
    db.init()
    try:
        accounts.init()
    except accounts.AccountError:
        pass  # reported on every request by access.EnforceAccess, which stays locked
    retention.start_sweeper()
    # Private boots (smoke tests, exports) are transient units, not our children: a
    # crash or restart of this app would otherwise leave them holding ports.
    sandbox.unboot_all()
    yield
    otreeapi.pool.shutdown()
    if config.dry_run_serves():
        # Nothing outlives us in a served dry run: the experiment is our child process,
        # not a systemd unit, so leaving it running would strand it holding the port.
        systemd.reset_fake_state()


app = FastAPI(title="otreedeploy", docs_url=None, redoc_url=None, openapi_url=None,
              lifespan=_lifespan)
# Wraps every route and the static mount, so a page added later is protected by default
# rather than by remembering to protect it.
app.add_middleware(access.EnforceAccess)
# Added last, so it runs first: a forged write is refused before anything else.
app.add_middleware(auth.SameOriginWrites)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


def _filesize(value) -> str:
    if value is None:
        return "—"
    size = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def _datetime(value) -> str:
    if not value:
        return "—"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(value))


templates.env.filters["filesize"] = _filesize
templates.env.filters["datetime"] = _datetime




# ---------------------------------------------------------------------------- helpers


# Notices and errors travel in the redirect's query string, so that a reload or a
# bookmarked page does not depend on server-side state. Left unsigned, that would let any
# link make this app display any text it likes -- "your password has expired, enter it
# at ..." -- with the app's own styling. Each message therefore carries an HMAC under a
# per-process key; one that does not verify is simply not shown. A restart drops a
# pending message, which costs nothing.
_MESSAGE_KEY = secrets.token_bytes(32)
_MESSAGE_KINDS = ("notice", "error")


def _message_signature(kind: str, message: str) -> str:
    mac = hmac.new(_MESSAGE_KEY, f"{kind}\0{message}".encode(), hashlib.sha256)
    return mac.hexdigest()[:32]


def _with_message(target: str, message: str | None, error: bool = False) -> str:
    if not message:
        return target
    kind = "error" if error else "notice"
    query = urlencode({kind: message, "sig": _message_signature(kind, message)}, quote_via=quote)
    return f"{target}?{query}"


def _signed_messages(request: Request) -> dict:
    """The notice/error in this request's URL, if and only if this app put it there."""
    signature = request.query_params.get("sig", "")
    found = {}
    for kind in _MESSAGE_KINDS:
        message = request.query_params.get(kind)
        if message and hmac.compare_digest(signature, _message_signature(kind, message)):
            found[kind] = message
    return found


def _render(request: Request, template: str, **context) -> HTMLResponse:
    # A message the handler passes directly is trusted; one from the URL must verify.
    for kind, message in _signed_messages(request).items():
        context.setdefault(kind, message)
    # Which project holds port 80 is shown by name only to those who may see it; others
    # learn that another group's experiment is live, and whom to ask about it.
    active = deploy.active_slug()
    user = access.current_user(request)
    hidden = bool(active and not accounts.can_access(user, active))
    return templates.TemplateResponse(
        request=request,
        name=template,
        context={
            "active_slug": None if hidden else active,
            "other_live": hidden,
            "live_contacts": ", ".join(accounts.members_of(active)) if hidden else "",
            **context,
        },
    )


def _back(slug: str | None = None, message: str | None = None, error: bool = False) -> RedirectResponse:
    target = f"/p/{slug}" if slug else "/"
    return RedirectResponse(_with_message(target, message, error), status_code=303)


def _msg(request: Request, text: str, **params) -> str:
    """Translate a message that carries values before it leaves Python.

    Templates translate their own literals, and a plain notice such as "restarted" can
    travel as English and be looked up on the way out. One carrying a number cannot: by
    the time the template sees it the value is already substituted and no catalogue key
    matches. So these are translated here, and t() in the template passes them through.
    """
    return i18n.translate(_language(request), text, **params)


def _project_or_404(slug: str):
    try:
        projects.validate(slug)
    except projects.InvalidSlug:
        return None
    return db.get_project(slug)


def _overview(slug: str, active: str | None = None) -> dict:
    """Everything the list and detail pages show about one project.

    The listing renders this for every project, so it reads the oTree version recorded at
    deploy time rather than importing otree in a subprocess to ask, and takes its data
    counts over a single database connection.
    """
    project = db.get_project(slug)
    release_dir = deploy.current_release_dir(slug)
    release = release_dir.name if release_dir else None
    record = db.get_release(slug, release) if release else None
    stats = postgres.project_stats(slug)
    return {
        "project": project,
        "status": deploy.status_of(slug),
        "is_active": (active if active is not None else deploy.active_slug()) == slug,
        "release": release,
        "otree_version": record.otree_version if record else None,
        "participants": stats.active_participants,
        "session_count": stats.session_count,
        "db_size": postgres.database_size(slug),
        "has_data": stats.has_tables,
    }


def _handle(action, slug: str, success: str):
    """Run a lifecycle action, turning the two expected failures into readable pages."""
    try:
        action()
    except ParticipantsActive as exc:
        return _back(slug, str(exc), error=True)
    except (DeployError, systemd.SystemdError, postgres.PostgresError) as exc:
        return _back(slug, str(exc), error=True)
    return _back(slug, success)


# ------------------------------------------------------------------------------ pages


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    user = _user(request)
    active = deploy.active_slug()
    visible = [p for p in db.list_projects() if accounts.can_access(user, p.slug)]
    overviews = [_overview(p.slug, active=active) for p in visible]
    return _render(
        request,
        "index.html",
        overviews=overviews,
        events=[e for e in db.recent_events(limit=30)
                if e["slug"] is None and user.is_admin or accounts.can_access(user, e["slug"])][:8],
    )


@app.get("/lang/{code}")
def set_language(code: str, request: Request, next: str = "/"):
    """Switch language and return to the page you were on."""
    # Only ever redirect somewhere inside this app: "//host" is a protocol-relative URL
    # that would leave the site entirely.
    target = next if next.startswith("/") and not next.startswith("//") else "/"
    response = RedirectResponse(target, status_code=303)
    response.set_cookie(
        i18n.COOKIE,
        i18n.normalize(code),
        max_age=i18n.COOKIE_MAX_AGE,
        samesite="lax",
        path="/",
    )
    return response


# ---------------------------------------------------------------------- signing in


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request, next: str = "/"):
    if access.current_user(request) is not None:
        return RedirectResponse(auth.safe_next(next), status_code=303)
    return _render(request, "login.html", next=auth.safe_next(next))


@app.post("/login")
def login(
    request: Request,
    username: str = Form(default=""),
    password: str = Form(default=""),
    next: str = Form(default="/"),
):
    target = auth.safe_next(next)
    name = username.strip().lower()
    # Keyed on address and name together: one person's typos lock out only that name
    # from that machine, not every colleague in the room.
    key = f"{_client(request)}|{name}"
    wait = auth.locked_for(key)
    if wait:
        return _render(
            request,
            "login.html",
            next=target,
            username=name,
            error=_msg(request, "too many attempts; try again in {n} seconds", n=wait),
        )

    user = accounts.verify(name, password)
    if user is None:
        auth.note_failure(key)
        audit.record("sign_in_failed", name or "-", None, client=_client(request))
        return _render(request, "login.html", next=target, username=name,
                       error="wrong user name or password")

    auth.note_success(key)
    audit.record("sign_in", user.name, None, client=_client(request))
    response = RedirectResponse(target, status_code=303)
    _set_session(response, accounts.start_session(user.name))
    return response


@app.post("/logout")
def logout(request: Request):
    user = access.current_user(request)
    accounts.end_session(request.cookies.get(access.COOKIE))
    if user is not None and user is not accounts.DEV_USER:
        _audit(request, "sign_out")
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(access.COOKIE, path="/")
    return response


def _set_session(response: Response, token: str) -> None:
    response.set_cookie(
        access.COOKIE,
        token,
        max_age=accounts.SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
        path="/",
    )


@app.get("/account", response_class=HTMLResponse)
def account_page(request: Request):
    user = _user(request)
    projects_ = sorted(accounts.projects_of(user.name))
    return _render(request, "account.html", projects=projects_)


@app.post("/account/password")
def change_password(
    request: Request,
    current_password: str = Form(default=""),
    new_password: str = Form(default=""),
    confirm_password: str = Form(default=""),
):
    """Change your own password. Every other session of yours ends."""
    user = _user(request)
    back = lambda message, error=False: RedirectResponse(  # noqa: E731
        _with_message("/account", message, error), status_code=303)
    if user is accounts.DEV_USER:
        return back("a dry run without accounts has no password to change", error=True)
    if accounts.verify(user.name, current_password) is None:
        return back("the current password is not right", error=True)
    if new_password != confirm_password:
        return back("the new passwords did not match", error=True)
    try:
        accounts.set_password(user.name, new_password)
    except accounts.AccountError as exc:
        return back(str(exc), error=True)
    _audit(request, "password_changed")
    response = back("password changed; your other sessions have been signed out")
    # Changing the password ended every session of this user, this one included; hand
    # this browser a fresh one rather than bouncing the person who just proved it.
    _set_session(response, accounts.start_session(user.name))
    return response


def _system_back(message: str, error: bool = False) -> RedirectResponse:
    return RedirectResponse(_with_message("/system", message, error), status_code=303)


# ------------------------------------------------------------------------------ pages


@app.get("/upload", response_class=HTMLResponse)
def upload_form(request: Request, slug: str | None = None):
    project = _project_or_404(slug or "")
    if project is not None and not accounts.can_access(_user(request), project.slug):
        return _back(None, "there is no such project", error=True)
    participants = (
        postgres.count_active_participants(project.slug)
        if project and deploy.status_of(project.slug) == "running"
        else 0
    )
    return _render(
        request, "upload.html", slug=slug, project=project, participants=participants
    )


@app.post("/upload")
async def upload(
    request: Request,
    file: UploadFile,
    slug: str | None = Form(default=None),
    force: bool = Form(default=False),
):
    filename = file.filename or "project.otreezip"
    user = _user(request)
    slug = (slug or "").strip() or None
    if slug is not None:
        # A new version of an existing project: only its members may replace its code.
        if not accounts.can_access(user, slug) or db.get_project(slug) is None:
            await file.close()
            return _back(None, "there is no such project", error=True)
    else:
        # A new upload is named after its file. If that name belongs to a project this
        # person is a member of, it is a new version of it (as before accounts); if it
        # belongs to somebody else's, this becomes a separate project rather than
        # touching theirs -- and without saying whose name was taken.
        existing = {p.slug for p in db.list_projects()}
        desired = projects.slugify(filename)
        if desired in existing and accounts.can_access(user, desired):
            slug = desired
        else:
            slug = projects.unique_slug(desired, existing)
    is_new = db.get_project(slug) is None

    # Stream to disk rather than into memory: these are whole projects.
    tmp_dir = Path(tempfile.mkdtemp(prefix="otreedeploy-upload-"))
    tmp_path = tmp_dir / "upload.otreezip"
    size = 0
    try:
        with open(tmp_path, "wb") as out:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > config.MAX_UPLOAD_BYTES:
                    shutil.rmtree(tmp_dir, ignore_errors=True)
                    return _back(
                        slug,
                        f"that file is larger than "
                        f"{config.MAX_UPLOAD_BYTES // (1024 * 1024)} MB",
                        error=True,
                    )
                out.write(chunk)
    finally:
        await file.close()

    if size == 0:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return _back(slug, "no file was uploaded", error=True)

    job = deploy.new_job(f"Deploying {filename}", slug, user=user.name)
    if is_new and user is not accounts.DEV_USER:
        accounts.grant(user.name, slug)
    _audit(request, "upload", slug, f"{filename} ({size} bytes)" + (" (forced)" if force else ""))

    def run() -> None:
        try:
            job.log(f"Received {filename} ({size / 1024:.0f} KB)")
            deploy.deploy(job, tmp_path, filename, slug=slug, force=force)
            job.log("")
            job.log("Done.")
            job.finish("success")
        except ParticipantsActive as exc:
            job.log("")
            job.log(f"Stopped: {exc}")
            job.finish("blocked", str(exc))
        except (DeployError, ArchiveError) as exc:
            job.log("")
            job.log(f"Failed: {exc}")
            job.finish("live_down" if job.live_down else "failed", str(exc))
        except Exception as exc:  # unexpected: still needs to reach the user
            job.log("")
            job.log(f"Unexpected error: {exc!r}")
            job.finish("live_down" if job.live_down else "failed", repr(exc))
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    # The deploy's history entries are attributed to whoever started it.
    context = contextvars.copy_context()
    threading.Thread(target=context.run, args=(run,), daemon=True).start()
    return RedirectResponse(f"/jobs/{job.id}", status_code=303)


def _job_for(request: Request, job_id: str) -> deploy.Job | None:
    """A deploy log, if this user may see it: the one who started it, or a member."""
    job = deploy.get_job(job_id)
    user = _user(request)
    if job is None:
        return None
    if job.user == user.name or accounts.can_access(user, job.slug):
        return job
    return None


@app.get("/jobs/{job_id}", response_class=HTMLResponse)
def job_page(request: Request, job_id: str):
    job = _job_for(request, job_id)
    if job is None:
        return _back(None, "that deploy log has expired", error=True)
    return _render(request, "job.html", job=job)


@app.get("/jobs/{job_id}/stream")
def job_stream(request: Request, job_id: str):
    job = _job_for(request, job_id)
    if job is None:
        return PlainTextResponse("unknown job", status_code=404)

    def events() -> Iterator[str]:
        for line in job.subscribe():
            yield _sse(line)
        yield _sse(f"__status__:{job.status}", event="done")

    return StreamingResponse(events(), media_type="text/event-stream", headers=_SSE_HEADERS)


@app.get("/p/{slug}", response_class=HTMLResponse)
def project_page(request: Request, slug: str):
    project = _project_or_404(slug)
    if project is None:
        return _back(None, f"no project called '{slug}'", error=True)
    return _render(
        request,
        "project.html",
        releases=db.list_releases(slug, limit=20),
        sessions=postgres.list_sessions(slug),
        backups=_backups(slug),
        auth_levels=environment.AUTH_LEVELS,
        incumbent_participants=_incumbent_participants(slug),
        may_force_incumbent=_may_force_incumbent(request, slug),
        members=accounts.members_of(slug),
        addable=[u.name for u in accounts.list_users()
                 if not u.disabled and u.name not in accounts.members_of(slug)],
        history=audit.read(limit=30, project=slug),
        **_overview(slug),
    )


def _may_force_incumbent(request: Request, slug: str) -> bool:
    """Interrupting the live project's participants is a decision for its own people."""
    incumbent = deploy.active_slug()
    return not incumbent or incumbent == slug or accounts.can_access(_user(request), incumbent)


def _incumbent_participants(slug: str) -> int:
    """Participants that activating ``slug`` would interrupt: the live project's."""
    incumbent = deploy.active_slug()
    if not incumbent or incumbent == slug or not systemd.is_active(incumbent):
        return 0
    return postgres.count_active_participants(incumbent)


def _backups(slug: str) -> list[dict]:
    directory = config.get_settings().backup_dir(slug)
    if not directory.exists():
        return []
    entries = []
    for path in sorted(directory.iterdir(), reverse=True):
        if path.suffix == ".gz":
            stat = path.stat()
            entries.append(
                {
                    "name": path.name,
                    "size": stat.st_size,
                    "when": time.strftime("%Y-%m-%d %H:%M", time.localtime(stat.st_mtime)),
                }
            )
    return entries[:20]


# ------------------------------------------------------------------------- lifecycle


def _forced(force: bool) -> str:
    return " (overriding the participant guard)" if force else ""


@app.post("/p/{slug}/activate")
def activate(request: Request, slug: str, force: bool = Form(default=False)):
    incumbent = deploy.active_slug()
    if force and not _may_force_incumbent(request, slug):
        contacts = ", ".join(accounts.members_of(incumbent)) or "an admin"
        return _back(
            slug,
            "the live experiment belongs to another group, and only its members can "
            f"interrupt its participants. Ask one of them ({contacts}), or wait until "
            "nobody is active",
            error=True,
        )
    _audit(request, "activate", slug,
           (f"replacing {incumbent}" if incumbent and incumbent != slug else "") + _forced(force))
    return _handle(
        lambda: deploy.activate(slug, force=force),
        slug,
        "now serving on port {port}",
    )


@app.post("/p/{slug}/stop")
def stop(request: Request, slug: str, force: bool = Form(default=False)):
    _audit(request, "stop", slug, _forced(force))
    return _handle(lambda: deploy.stop(slug, force=force), slug, "stopped")


@app.post("/p/{slug}/restart")
def restart(request: Request, slug: str, force: bool = Form(default=False)):
    _audit(request, "restart", slug, _forced(force))
    return _handle(lambda: deploy.restart(slug, force=force), slug, "restarted")


@app.post("/p/{slug}/rollback")
def rollback(request: Request, slug: str, release: str = Form(...),
             force: bool = Form(default=False)):
    _audit(request, "rollback", slug, f"to {release}{_forced(force)}")
    return _handle(
        lambda: deploy.rollback(slug, release, force=force),
        slug,
        f"rolled back to {release}",
    )


@app.post("/p/{slug}/settings")
def update_settings(
    request: Request,
    slug: str,
    display_name: str = Form(...),
    admin_password: str = Form(...),
    auth_level: str = Form(...),
):
    project = _project_or_404(slug)
    if project is None:
        return _back(None, f"no project called '{slug}'", error=True)
    if auth_level not in environment.AUTH_LEVELS:
        return _back(slug, f"unknown access level '{auth_level}'", error=True)
    if not admin_password.strip():
        return _back(slug, "the admin password cannot be empty", error=True)

    db.update_project(
        slug,
        display_name=display_name.strip() or slug,
        admin_password=admin_password.strip(),
        auth_level=auth_level,
    )
    updated = db.get_project(slug)
    assert updated is not None
    environment.write(updated)
    _audit(request, "settings", slug, f"access level {auth_level}")
    otreeapi.pool.release(slug)

    if deploy.status_of(slug) == "running":
        return _handle(
            lambda: deploy.restart(slug, force=False), slug, "settings saved and applied"
        )
    return _back(slug, "settings saved; they apply next time it starts")


@app.post("/p/{slug}/reset-db")
def reset_db(request: Request, slug: str, confirm: str = Form(default=""),
             force: bool = Form(default=False)):
    if confirm.strip() != slug:
        return _back(
            slug, "type the project name exactly to confirm the reset", error=True
        )
    _audit(request, "reset_database", slug, _forced(force))
    try:
        snapshot = deploy.reset_database(slug, force=force)
    except ParticipantsActive as exc:
        return _back(slug, str(exc), error=True)
    except (DeployError, postgres.PostgresError) as exc:
        return _back(slug, str(exc), error=True)
    where = f"; backup saved as {Path(snapshot).name}" if snapshot else ""
    return _back(slug, f"database reset{where}")


@app.post("/p/{slug}/restore")
def restore_backup(
    request: Request,
    slug: str,
    backup: str = Form(default=""),
    confirm: str = Form(default=""),
    force: bool = Form(default=False),
):
    if confirm.strip() != slug:
        return _back(slug, "type the project name exactly to confirm the restore", error=True)
    _audit(request, "restore", slug, backup + _forced(force))
    try:
        snapshot = deploy.restore_backup(slug, backup, force=force)
    except ParticipantsActive as exc:
        return _back(slug, str(exc), error=True)
    except (DeployError, postgres.PostgresError) as exc:
        return _back(slug, str(exc), error=True)
    where = f"; the data it replaced was saved as {Path(snapshot).name}" if snapshot else ""
    return _back(slug, f"restored {backup}{where}")


@app.post("/p/{slug}/delete")
def delete(
    request: Request,
    slug: str,
    confirm: str = Form(default=""),
    force: bool = Form(default=False),
    purge: bool = Form(default=False),
    confirm_purge: str = Form(default=""),
):
    if confirm.strip() != slug:
        return _back(
            slug, "type the project name exactly to confirm deletion", error=True
        )
    if purge and confirm_purge.strip() != "delete all data":
        return _back(
            slug,
            "to delete the backups and uploads as well, also type: delete all data",
            error=True,
        )
    _audit(request, "delete", slug,
           ("including all backups and uploads" if purge else "backups kept") + _forced(force))
    try:
        snapshot = deploy.delete_project(slug, force=force, purge=purge)
    except ParticipantsActive as exc:
        return _back(slug, str(exc), error=True)
    except (DeployError, postgres.PostgresError) as exc:
        return _back(slug, str(exc), error=True)
    if purge:
        return _back(None, f"deleted '{slug}' and every backup and upload of it.")
    where = (f" A final backup was kept as {Path(snapshot).name}; an admin can find it "
             "under deleted projects on the System page.") if snapshot else ""
    return _back(None, f"deleted '{slug}'.{where}")


@app.post("/p/{slug}/members/add")
def add_member(request: Request, slug: str, name: str = Form(default="")):
    try:
        accounts.grant(name.strip(), slug)
    except accounts.AccountError as exc:
        return _back(slug, str(exc), error=True)
    _audit(request, "access_granted", slug, name)
    return _back(slug, f"{name} can now see and manage this project")


@app.post("/p/{slug}/members/remove")
def remove_member(request: Request, slug: str, name: str = Form(default="")):
    accounts.revoke(name, slug)
    _audit(request, "access_revoked", slug, name)
    return _back(slug, f"{name} no longer has access to this project")


# ------------------------------------------------------------------------------ logs

_SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


def _sse(data: str, event: str | None = None) -> str:
    prefix = f"event: {event}\n" if event else ""
    # Every line of a multi-line payload needs its own data: field.
    body = "".join(f"data: {line}\n" for line in data.split("\n"))
    return f"{prefix}{body}\n"


@app.get("/p/{slug}/logs", response_class=PlainTextResponse)
def logs(slug: str, lines: int = 300):
    if _project_or_404(slug) is None:
        return PlainTextResponse("unknown project", status_code=404)
    return systemd.journal(slug, lines=lines)


@app.get("/p/{slug}/logs/stream")
def logs_stream(slug: str):
    if _project_or_404(slug) is None:
        return PlainTextResponse("unknown project", status_code=404)

    def events() -> Iterator[str]:
        for line in systemd.follow_journal(slug):
            yield _sse(line)

    return StreamingResponse(events(), media_type="text/event-stream", headers=_SSE_HEADERS)


# ------------------------------------------------------------------------------- data


@app.get("/p/{slug}/data", response_class=HTMLResponse)
def data_page(request: Request, slug: str):
    project = _project_or_404(slug)
    if project is None:
        return _back(None, f"no project called '{slug}'", error=True)
    return _render(
        request,
        "data.html",
        project=project,
        sessions=postgres.list_sessions(slug),
        has_data=postgres.has_otree_tables(slug),
        status=deploy.status_of(slug),
    )


@app.get("/p/{slug}/data/{kind}.csv")
def download_csv(request: Request, slug: str, kind: str, session_code: str | None = None):
    """Export data from any project, running or not.

    If the project is not the live one, it is booted on a loopback port just long
    enough to answer, then left in a pool so a run of downloads reuses the one boot.
    """
    project = _project_or_404(slug)
    if project is None:
        return PlainTextResponse("unknown project", status_code=404)
    if kind not in otreeapi.EXPORTS:
        return PlainTextResponse(f"unknown export type '{kind}'", status_code=400)

    if deploy.status_of(slug) == "running" and health.responds(config.otree_port()):
        port = config.otree_port()
    else:
        release_dir = deploy.current_release_dir(slug)
        if release_dir is None:
            return PlainTextResponse("this project has no deployed release", status_code=409)
        try:
            port = otreeapi.pool.acquire(project, release_dir).port
        except otreeapi.OtreeError as exc:
            return PlainTextResponse(f"could not start the project: {exc}", status_code=500)

    try:
        body, stem = otreeapi.export_csv(port, project.rest_key, kind, session_code)
    except otreeapi.OtreeError as exc:
        return PlainTextResponse(str(exc), status_code=502)

    suffix = f"-{session_code}" if session_code else ""
    filename = f"{slug}-{stem}{suffix}-{time.strftime('%Y%m%d')}.csv"
    _audit(request, "download_data", slug,
           f"{kind}" + (f", session {session_code}" if session_code else ", all sessions"))
    return Response(
        content=body,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/p/{slug}/backups/{name}")
def download_backup(request: Request, slug: str, name: str):
    if _project_or_404(slug) is None:
        return PlainTextResponse("unknown project", status_code=404)
    directory = config.get_settings().backup_dir(slug)
    target = (directory / name).resolve()
    # Never let a crafted name walk out of the backup directory.
    if not target.is_relative_to(directory.resolve()) or not target.is_file():
        return PlainTextResponse("no such backup", status_code=404)
    _audit(request, "download_backup", slug, name)
    # Streamed from disk, not read into memory: a backup can be large (N4).
    return FileResponse(target, media_type="application/gzip", filename=name)


# ----------------------------------------------------------------------------- system


def _existing_ancestor(path: Path) -> str:
    """Nearest directory that exists, so disk stats work before the tree is built."""
    for candidate in [path, *path.parents]:
        if candidate.is_dir():
            return str(candidate)
    return "/"


@app.get("/system", response_class=HTMLResponse)
def system(request: Request):
    usage = shutil.disk_usage(_existing_ancestor(config.get_settings().otree_home))
    return _render(
        request,
        "system.html",
        disk={
            "total": usage.total,
            "used": usage.used,
            "free": usage.free,
            "percent": round(usage.used / usage.total * 100) if usage.total else 0,
        },
        postgres_version=postgres.server_version(),
        otree_port_responding=health.responds(config.otree_port()),
        dry_run=config.dry_run(),
        settings=config.get_settings(),
        events=db.recent_events(limit=40),
        retention=retention.get(),
        deleted=retention.deleted_archives(),
        audit_entries=audit.read(
            limit=200,
            project=request.query_params.get("project") or None,
            user=request.query_params.get("user") or None,
        ),
        audit_filter={"project": request.query_params.get("project", ""),
                      "user": request.query_params.get("user", "")},
    )


def _days(value: str) -> int | None:
    value = value.strip()
    return int(value) if value else None


@app.post("/system/retention")
def set_retention(request: Request, backups: str = Form(default=""), uploads: str = Form(default="")):
    try:
        retention.set_periods(_days(backups), _days(uploads))
    except ValueError as exc:
        return _system_back(f"not saved: {exc}", error=True)
    _audit(request, "retention_set", None, f"backups {backups or 'keep'}, uploads {uploads or 'keep'}")
    removed = retention.sweep()
    return _system_back(f"retention saved; {len(removed)} file(s) past it were deleted")


@app.post("/system/deleted/remove")
def remove_deleted(request: Request, name: str = Form(default=""),
                   confirm: str = Form(default="")):
    if confirm.strip() != name:
        return _system_back("type the name exactly to confirm", error=True)
    try:
        retention.remove_archive(name)
    except ValueError as exc:
        return _system_back(str(exc), error=True)
    _audit(request, "delete_archive", None, name)
    return _system_back(f"deleted the remaining backups and uploads of {name}")


# ------------------------------------------------------------------------------ users


@app.get("/users", response_class=HTMLResponse)
def users_page(request: Request):
    rows = [{"user": u, "projects": sorted(accounts.projects_of(u.name))}
            for u in accounts.list_users()]
    return _render(request, "users.html", rows=rows, one_time=None)


def _users_back(message: str, error: bool = False) -> RedirectResponse:
    return RedirectResponse(_with_message("/users", message, error), status_code=303)


def _one_time(request: Request, name: str, password: str, message: str) -> HTMLResponse:
    """Show a generated password exactly once, on this page, never in a URL."""
    rows = [{"user": u, "projects": sorted(accounts.projects_of(u.name))}
            for u in accounts.list_users()]
    return _render(request, "users.html", rows=rows,
                   one_time={"name": name, "password": password}, notice=message)


@app.post("/users/add")
def add_user(request: Request, name: str = Form(default=""),
             is_admin: bool = Form(default=False)):
    name = name.strip().lower()
    password = accounts.generate_password()
    try:
        accounts.create_user(name, password, is_admin=is_admin)
    except accounts.AccountError as exc:
        return _users_back(str(exc), error=True)
    _audit(request, "user_created", None, name + (" (admin)" if is_admin else ""))
    return _one_time(request, name, password, f"created {name}")


@app.post("/users/{name}/password")
def reset_user_password(request: Request, name: str):
    password = accounts.generate_password()
    try:
        accounts.set_password(name, password)
    except accounts.AccountError as exc:
        return _users_back(str(exc), error=True)
    _audit(request, "user_password_reset", None, name)
    return _one_time(request, name, password, f"new password for {name}; their sessions have ended")


@app.post("/users/{name}/admin")
def set_user_admin(request: Request, name: str, on: bool = Form(default=False)):
    try:
        accounts.set_admin(name, on)
    except accounts.AccountError as exc:
        return _users_back(str(exc), error=True)
    _audit(request, "user_admin", None, f"{name} {'on' if on else 'off'}")
    return _users_back(f"{name} is {'now' if on else 'no longer'} an admin")


@app.post("/users/{name}/disable")
def set_user_disabled(request: Request, name: str, disabled: bool = Form(default=False)):
    if disabled and name == _user(request).name:
        return _users_back("you cannot disable your own account", error=True)
    try:
        accounts.set_disabled(name, disabled)
    except accounts.AccountError as exc:
        return _users_back(str(exc), error=True)
    _audit(request, "user_disabled" if disabled else "user_enabled", None, name)
    return _users_back(f"{name} is {'disabled' if disabled else 'enabled again'}")
