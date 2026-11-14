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
    PlainTextResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import (
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
    """What every page needs to know about signing in.

    ``auth_required`` is false only until somebody sets a password, which bootstrap.sh
    does; the templates use it to keep nagging until that happens.
    """
    try:
        signed_in = auth.check_token(request.cookies.get(auth.COOKIE))
    except auth.AuthError:
        signed_in = False
    return {
        "auth_required": auth.is_configured(),
        "signed_in": signed_in,
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

@asynccontextmanager
async def _lifespan(_: FastAPI):
    db.init()
    # Private boots (smoke tests, exports) are transient units, not our children: a
    # crash or restart of this app would otherwise leave them holding ports.
    sandbox.unboot_all()
    yield
    otreeapi.pool.shutdown()
    if config.dry_run_serves():
        # Nothing outlives us in a served dry run: the experiment is our child process,
        # not a systemd unit, so leaving it running would strand it holding the port.
        systemd.reset_fake_state()


app = FastAPI(title="otreedeploy", docs_url=None, redoc_url=None, lifespan=_lifespan)
# Wraps every route and the static mount, so a page added later is protected by default
# rather than by remembering to protect it.
app.add_middleware(auth.RequireLogin)
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
    return templates.TemplateResponse(
        request=request,
        name=template,
        context={"active_slug": deploy.active_slug(), **context},
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
    active = deploy.active_slug()
    overviews = [_overview(p.slug, active=active) for p in db.list_projects()]
    return _render(
        request,
        "index.html",
        overviews=overviews,
        events=db.recent_events(limit=8),
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
    if not auth.is_configured() or auth.check_token(request.cookies.get(auth.COOKIE)):
        return RedirectResponse(auth.safe_next(next), status_code=303)
    return _render(request, "login.html", next=auth.safe_next(next))


@app.post("/login")
def login(
    request: Request,
    password: str = Form(default=""),
    next: str = Form(default="/"),
):
    target = auth.safe_next(next)
    if not auth.is_configured():
        return RedirectResponse(target, status_code=303)

    client = request.client.host if request.client else "unknown"
    wait = auth.locked_for(client)
    if wait:
        return _render(
            request,
            "login.html",
            next=target,
            error=_msg(request, "too many attempts; try again in {n} seconds", n=wait),
        )

    if not auth.verify_password(password):
        auth.note_failure(client)
        return _render(request, "login.html", next=target, error="wrong password")

    auth.note_success(client)
    response = RedirectResponse(target, status_code=303)
    _set_session(response)
    return response


@app.post("/logout")
def logout():
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(auth.COOKIE, path="/")
    return response


def _set_session(response: Response) -> None:
    response.set_cookie(
        auth.COOKIE,
        auth.issue_token(),
        max_age=auth.SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
        path="/",
    )


@app.post("/password")
def change_password(
    request: Request,
    current_password: str = Form(default=""),
    new_password: str = Form(default=""),
    confirm_password: str = Form(default=""),
):
    """Change the password from the System page.

    The first password is never set here: until one exists the app is open, so anyone on
    the network could set one and lock the experimenter out. That one has to be set on
    the box, which is what bootstrap.sh does.
    """
    if not auth.is_configured():
        return _system_back("no password is set yet; set the first one on the server", error=True)
    if not auth.verify_password(current_password):
        return _system_back("the current password is not right", error=True)
    if new_password != confirm_password:
        return _system_back("the new passwords did not match", error=True)
    if len(new_password) < auth.MIN_PASSWORD_LENGTH:
        return _system_back(
            _msg(
                request,
                "the password must be at least {n} characters long",
                n=auth.MIN_PASSWORD_LENGTH,
            ),
            error=True,
        )
    try:
        auth.set_password(new_password)
    except (ValueError, OSError) as exc:
        return _system_back(str(exc), error=True)

    auth.reset_rate_limits()
    # Changing the password rotates the signing secret, so this browser's cookie died
    # along with everyone else's. Hand it a fresh one rather than bouncing the person
    # who just proved they know the password.
    response = _system_back("password changed; every other browser has been signed out")
    _set_session(response)
    return response


def _system_back(message: str, error: bool = False) -> RedirectResponse:
    return RedirectResponse(_with_message("/system", message, error), status_code=303)


# ------------------------------------------------------------------------------ pages


@app.get("/upload", response_class=HTMLResponse)
def upload_form(request: Request, slug: str | None = None):
    project = _project_or_404(slug or "")
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

    job = deploy.new_job(f"Deploying {filename}", slug)

    def run() -> None:
        try:
            job.log(f"Received {filename} ({size / 1024:.0f} KB)")
            deploy.deploy(job, tmp_path, filename, slug=slug or None, force=force)
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

    threading.Thread(target=run, daemon=True).start()
    return RedirectResponse(f"/jobs/{job.id}", status_code=303)


@app.get("/jobs/{job_id}", response_class=HTMLResponse)
def job_page(request: Request, job_id: str):
    job = deploy.get_job(job_id)
    if job is None:
        return _back(None, "that deploy log has expired", error=True)
    return _render(request, "job.html", job=job)


@app.get("/jobs/{job_id}/stream")
def job_stream(job_id: str):
    job = deploy.get_job(job_id)
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
        **_overview(slug),
    )


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


@app.post("/p/{slug}/activate")
def activate(slug: str, force: bool = Form(default=False)):
    return _handle(
        lambda: deploy.activate(slug, force=force),
        slug,
        "now serving on port {port}",
    )


@app.post("/p/{slug}/stop")
def stop(slug: str, force: bool = Form(default=False)):
    return _handle(lambda: deploy.stop(slug, force=force), slug, "stopped")


@app.post("/p/{slug}/restart")
def restart(slug: str, force: bool = Form(default=False)):
    return _handle(lambda: deploy.restart(slug, force=force), slug, "restarted")


@app.post("/p/{slug}/rollback")
def rollback(slug: str, release: str = Form(...), force: bool = Form(default=False)):
    return _handle(
        lambda: deploy.rollback(slug, release, force=force),
        slug,
        f"rolled back to {release}",
    )


@app.post("/p/{slug}/settings")
def update_settings(
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
    otreeapi.pool.release(slug)

    if deploy.status_of(slug) == "running":
        return _handle(
            lambda: deploy.restart(slug, force=False), slug, "settings saved and applied"
        )
    return _back(slug, "settings saved; they apply next time it starts")


@app.post("/p/{slug}/reset-db")
def reset_db(slug: str, confirm: str = Form(default=""), force: bool = Form(default=False)):
    if confirm.strip() != slug:
        return _back(
            slug, "type the project name exactly to confirm the reset", error=True
        )
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
    slug: str,
    backup: str = Form(default=""),
    confirm: str = Form(default=""),
    force: bool = Form(default=False),
):
    if confirm.strip() != slug:
        return _back(slug, "type the project name exactly to confirm the restore", error=True)
    try:
        snapshot = deploy.restore_backup(slug, backup, force=force)
    except ParticipantsActive as exc:
        return _back(slug, str(exc), error=True)
    except (DeployError, postgres.PostgresError) as exc:
        return _back(slug, str(exc), error=True)
    where = f"; the data it replaced was saved as {Path(snapshot).name}" if snapshot else ""
    return _back(slug, f"restored {backup}{where}")


@app.post("/p/{slug}/delete")
def delete(slug: str, confirm: str = Form(default=""), force: bool = Form(default=False)):
    if confirm.strip() != slug:
        return _back(
            slug, "type the project name exactly to confirm deletion", error=True
        )
    try:
        snapshot = deploy.delete_project(slug, force=force)
    except ParticipantsActive as exc:
        return _back(slug, str(exc), error=True)
    except (DeployError, postgres.PostgresError) as exc:
        return _back(slug, str(exc), error=True)
    where = f" A final backup was kept as {Path(snapshot).name}." if snapshot else ""
    return _back(None, f"deleted '{slug}'.{where}")


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
def download_csv(slug: str, kind: str, session_code: str | None = None):
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
    return Response(
        content=body,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/p/{slug}/backups/{name}")
def download_backup(slug: str, name: str):
    if _project_or_404(slug) is None:
        return PlainTextResponse("unknown project", status_code=404)
    directory = config.get_settings().backup_dir(slug)
    target = (directory / name).resolve()
    # Never let a crafted name walk out of the backup directory.
    if not str(target).startswith(str(directory.resolve()) + "/") or not target.is_file():
        return PlainTextResponse("no such backup", status_code=404)
    return Response(
        content=target.read_bytes(),
        media_type="application/gzip",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


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
    )
