"""Drive a throwaway server: a systemd container, and the management app inside it.

Nothing here simulates anything. Commands run in the container as the real users, HTTP
goes over the container's network to the real ports, and the database is PostgreSQL.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import httpx

RUNTIME = os.environ.get("OTREEDEPLOY_E2E_RUNTIME", "docker")
REPO = Path(__file__).resolve().parents[2]
PASSWORD = "e2e-correct-horse-battery"

BASE_IMAGE = "otreedeploy-e2e:base"
BOOTSTRAPPED_IMAGE = "otreedeploy-e2e:bootstrapped"
STATE_DB = "/var/lib/otreedeploy/state.db"
SRC_IN_CONTAINER = "/root/otreedeploy-src"


def runtime(*args: str, check: bool = True, timeout: float = 900, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(
        [RUNTIME, *args], check=check, timeout=timeout, text=True, capture_output=True, **kw
    )


class CommandFailed(AssertionError):
    pass


@dataclass
class Result:
    code: int
    out: str

    @property
    def ok(self) -> bool:
        return self.code == 0


class Server:
    """One container, booted with systemd."""

    def __init__(self, image: str, name: str | None = None) -> None:
        self.name = name or f"otreedeploy-e2e-{uuid.uuid4().hex[:8]}"
        runtime(
            "run", "-d", "--name", self.name, "--hostname", "labserver",
            "--privileged", "--cgroupns=private",
            "--tmpfs", "/run", "--tmpfs", "/run/lock",
            image,
        )
        self.wait_booted()

    # ------------------------------------------------------------------ lifecycle

    def wait_booted(self, timeout: float = 120) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            r = self.sh("systemctl is-system-running", check=False)
            if r.out.strip() in {"running", "degraded"}:
                return
            time.sleep(1)
        raise TimeoutError(f"{self.name} did not finish booting: {r.out!r}")

    def reboot(self) -> None:
        runtime("restart", "-t", "60", self.name)
        self.wait_booted()

    def stop(self) -> None:
        runtime("stop", "-t", "60", self.name)

    def remove(self) -> None:
        if os.environ.get("OTREEDEPLOY_E2E_KEEP"):
            print(f"\nkeeping container {self.name} (OTREEDEPLOY_E2E_KEEP is set)")
            return
        runtime("rm", "-f", self.name, check=False)

    def commit(self, image: str) -> None:
        runtime("commit", self.name, image)

    @property
    def ip(self) -> str:
        out = runtime(
            "inspect", "-f", "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}", self.name
        ).stdout.strip()
        assert out, f"{self.name} has no IP address"
        return out

    # ------------------------------------------------------------------- commands

    def sh(self, command: str, user: str | None = None, check: bool = True,
           timeout: float = 900, env: dict | None = None, stdin: str | None = None) -> Result:
        """Run a bash command in the container, as root unless `user` is given."""
        args = ["exec", "-i"]
        for key, value in (env or {}).items():
            args += ["-e", f"{key}={value}"]
        args.append(self.name)
        if user:
            args += ["runuser", "-u", user, "--", "bash", "-c", command]
        else:
            args += ["bash", "-c", command]
        proc = subprocess.run(
            [RUNTIME, *args], text=True, input=stdin, timeout=timeout,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        result = Result(proc.returncode, proc.stdout)
        if check and not result.ok:
            raise CommandFailed(f"`{command}` exited {proc.returncode}:\n{proc.stdout[-4000:]}")
        return result

    def copy_tree_in(self, source: Path, destination: str) -> None:
        """Copy a working tree in, leaving out what a release tarball would not contain."""
        self.sh(f"rm -rf {destination} && mkdir -p {destination}")
        tar = subprocess.Popen(
            ["tar", "-C", str(source), "--exclude=.venv", "--exclude=.git",
             "--exclude=__pycache__", "--exclude=.pytest_cache", "-cf", "-", "."],
            stdout=subprocess.PIPE,
        )
        subprocess.run(
            [RUNTIME, "exec", "-i", self.name, "tar", "-C", destination, "-xf", "-"],
            stdin=tar.stdout, check=True, timeout=300,
        )
        tar.wait()

    def put_file(self, local: Path, destination: str) -> None:
        runtime("cp", str(local), f"{self.name}:{destination}")

    def psql(self, database: str, sql: str) -> str:
        """Query as the postgres superuser, so the answer does not depend on our roles."""
        return self.sh(
            f"psql -tA -d {shlex.quote(database)} -c {shlex.quote(sql)}", user="postgres"
        ).out.strip()

    def state(self, sql: str) -> list[tuple]:
        """Read the management app's own SQLite state."""
        script = (
            "import json,sqlite3,sys;"
            f"c=sqlite3.connect('file:{STATE_DB}?mode=ro',uri=True);"
            "print(json.dumps(c.execute(sys.stdin.read()).fetchall()))"
        )
        out = self.sh(f"python3 -c {shlex.quote(script)}", stdin=sql).out
        return [tuple(row) for row in json.loads(out)]

    def project_secrets(self, slug: str) -> tuple[str, str]:
        rows = self.state(f"SELECT rest_key, admin_password FROM projects WHERE slug='{slug}'")
        assert rows, f"no project {slug!r} in state.db"
        return rows[0]

    def unit_active(self, slug: str) -> bool:
        return self.sh(f"systemctl is-active otree@{slug}", check=False).out.strip() == "active"

    def unit_enabled(self, slug: str) -> bool:
        return self.sh(f"systemctl is-enabled otree@{slug}", check=False).out.strip() == "enabled"

    def foreign_owned(self, slug: str) -> list[str]:
        """Objects in a project database not owned by the project's own role."""
        owner = f"otree_{slug}"
        out = self.psql(
            f"otree_{slug}",
            "SELECT c.relname || ':' || pg_get_userbyid(c.relowner) FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public' AND c.relkind IN ('r','S','v','m') "
            f"AND pg_get_userbyid(c.relowner) <> '{owner}' ORDER BY 1",
        )
        return [line for line in out.splitlines() if line]

    def copy_archive_in(self, tar_bytes: bytes, destination: str) -> None:
        """Unpack a tar stream (e.g. `git archive`) into the container."""
        self.sh(f"rm -rf {destination} && mkdir -p {destination}")
        subprocess.run(
            [RUNTIME, "exec", "-i", self.name, "tar", "-C", destination, "-xf", "-"],
            input=tar_bytes, check=True, timeout=300,
        )

    # ---------------------------------------------------------------------- HTTP

    def manager(self) -> "Manager":
        return Manager(f"http://{self.ip}:8000")

    def experiment(self, slug: str) -> "Experiment":
        rest_key, admin_password = self.project_secrets(slug)
        return Experiment(f"http://{self.ip}:80", rest_key, admin_password)


# ---------------------------------------------------------------------------- the app


@dataclass
class JobResult:
    id: str
    status: str
    log: str
    seconds: float

    def assert_success(self) -> "JobResult":
        assert self.status == "success", f"deploy ended {self.status!r}:\n{self.log}"
        return self


class Manager:
    """The management app, driven the way a browser would."""

    def __init__(self, base: str) -> None:
        self.base = base
        # A browser sends its page's origin with every form POST; the app refuses
        # writes without it (m1).
        self.http = httpx.Client(base_url=base, timeout=600, follow_redirects=False,
                                 headers={"Origin": base})

    def login(self, password: str = PASSWORD) -> "Manager":
        r = self.http.post("/login", data={"password": password, "next": "/"})
        assert r.status_code == 303 and "error" not in r.headers.get("location", ""), (
            f"sign-in failed: {r.status_code} {r.headers.get('location')}"
        )
        return self

    def upload(self, archive: Path, slug: str | None = None, force: bool = False,
               filename: str | None = None) -> JobResult:
        started = time.time()
        data = {"slug": slug or ""}
        if force:
            data["force"] = "true"
        r = self.http.post(
            "/upload",
            data=data,
            files={"file": (filename or archive.name, archive.read_bytes(), "application/gzip")},
        )
        location = r.headers.get("location", "")
        match = re.match(r"^/jobs/([0-9a-f]+)$", location)
        assert r.status_code == 303 and match, f"upload refused: {r.status_code} {location}"
        return self._follow_job(match.group(1), started)

    def _follow_job(self, job_id: str, started: float) -> JobResult:
        lines: list[str] = []
        status = "unknown"
        with self.http.stream("GET", f"/jobs/{job_id}/stream", timeout=900) as stream:
            event = None
            for raw in stream.iter_lines():
                if raw.startswith("event:"):
                    event = raw.split(":", 1)[1].strip()
                elif raw.startswith("data:"):
                    data = raw[5:].lstrip(" ")
                    if event == "done" and data.startswith("__status__:"):
                        status = data.split(":", 1)[1]
                        break
                    lines.append(data)
                elif raw == "":
                    event = None
        return JobResult(job_id, status, "\n".join(lines), time.time() - started)

    def post(self, path: str, **form) -> "Outcome":
        r = self.http.post(path, data={k: v for k, v in form.items() if v is not None})
        return Outcome.from_response(r)

    def get(self, path: str, **kw) -> httpx.Response:
        return self.http.get(path, **kw)

    def activate(self, slug: str, force: bool = False) -> "Outcome":
        return self.post(f"/p/{slug}/activate", force="true" if force else None)


@dataclass
class Outcome:
    """Where a lifecycle action redirected to, and whether it said it failed."""

    status_code: int
    location: str
    message: str
    error: bool

    @classmethod
    def from_response(cls, r: httpx.Response) -> "Outcome":
        from urllib.parse import parse_qs, urlparse

        location = r.headers.get("location", "")
        query = parse_qs(urlparse(location).query)
        if "error" in query:
            return cls(r.status_code, location, query["error"][0], True)
        message = query.get("notice", [""])[0]
        return cls(r.status_code, location, message, r.status_code >= 400)

    def assert_ok(self) -> "Outcome":
        assert not self.error, f"action failed: {self.message or self.status_code}"
        return self


# --------------------------------------------------------------------- the experiment


class Experiment:
    """The live oTree instance on port 80, driven as participants and as its REST API."""

    def __init__(self, base: str, rest_key: str, admin_password: str) -> None:
        self.base = base
        self.rest_key = rest_key
        self.admin_password = admin_password
        self.http = httpx.Client(base_url=base, timeout=120, follow_redirects=True)

    def create_session(self, config: str = "simple_task", participants: int = 2,
                       label: str = "e2e") -> httpx.Response:
        return self.http.post(
            "/api/sessions",
            json={"session_config_name": config, "num_participants": participants,
                  "label": label},
            headers={"otree-rest-key": self.rest_key},
        )

    def new_session(self, **kw) -> str:
        r = self.create_session(**kw)
        assert r.status_code == 200, f"session creation failed: {r.status_code} {r.text[:2000]}"
        return r.json()["code"]

    def participant_codes(self, session_code: str) -> list[str]:
        r = self.http.get(f"/api/sessions/{session_code}",
                          headers={"otree-rest-key": self.rest_key})
        assert r.status_code == 200, r.text[:2000]
        return [p["code"] for p in r.json()["participants"]]

    def open_as_participant(self, participant_code: str) -> httpx.Response:
        """What a participant's browser gets when they follow their link."""
        return self.http.get(f"/InitializeParticipant/{participant_code}")


def wait_for(predicate, timeout: float = 60, interval: float = 1.0, what: str = "condition"):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            last = predicate()
            if last:
                return last
        except (httpx.HTTPError, CommandFailed) as exc:
            last = exc
        time.sleep(interval)
    raise TimeoutError(f"timed out waiting for {what} (last: {last!r})")


def http_status(url: str) -> int | None:
    try:
        return httpx.get(url, timeout=10, follow_redirects=False).status_code
    except httpx.HTTPError:
        return None


# ------------------------------------------------------------------- common steps


def deploy_live(server: "Server", manager, archive: Path, metrics=None, sessions: int = 0,
                slug: str = "labdemo"):
    """Upload, activate, and optionally create sessions. Returns (experiment, codes)."""
    job = manager.upload(archive).assert_success()
    if metrics:
        metrics.time("upload_to_built_seconds", job.seconds)
    started = time.time()
    manager.activate(slug).assert_ok()
    wait_for(lambda: http_status(f"http://{server.ip}/") is not None,
             timeout=60, what="the experiment on port 80")
    if metrics:
        metrics.time("activate_seconds", time.time() - started)
    experiment = server.experiment(slug)
    codes = [experiment.new_session(label=f"s{i}") for i in range(sessions)]
    return experiment, codes
