"""HTTP API and web interface (FastAPI).

Reading is open – by default the server only listens on 127.0.0.1. Writing
requires a token in the header `Authorization: Bearer …`:

    admin token      create projects, renew project tokens, cancel jobs
    project token    submit (CI) and cancel jobs for exactly this project

Endpoints (OpenAPI under /api/docs):

    GET  /api/health
    GET  /api/projects                         overview with last job/gate
    POST /api/projects                         {key, name} → token (admin)
    GET  /api/projects/{key}                   project, branches, history of the gate measurements
    POST /api/projects/{key}/token             new project token (admin)
    GET  /api/projects/{key}/jobs              ?branch=&mode=&limit=
    POST /api/projects/{key}/jobs              multipart: archive, base_archive?, diff?, metadata (project token)
    GET  /api/jobs/{id}                        status, result, log
    GET  /api/jobs/{id}/sarif                  findings as SARIF 2.1.0 (code scanning)
    GET  /api/jobs/{id}/files/{path}           reports of gate/optimize (report.html, patches …)
    POST /api/jobs/{id}/cancel
    GET  /api/queue                            running and queued jobs
"""

# Without `from __future__ import annotations`: FastAPI has to evaluate the Annotated dependencies
# at runtime, and they refer to local functions in create_app().
import json
import logging
import os
import shutil
import stat
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from joulecode import __version__
from joulecode.server import MODES
from joulecode.server.processing import DummyProcessor, Processor
from joulecode.server.sarif import to_sarif
from joulecode.server.store import Project, Store, hash_token, new_token, token_matches
from joulecode.server.worker import Worker, job_dir

log = logging.getLogger("joulecode.server")
STATIC = Path(__file__).parent / "static"


@dataclass
class ServerConfig:
    data_dir: Path
    admin_token: str
    max_upload_mb: int = 200
    processor: Processor = field(default_factory=DummyProcessor)
    start_worker: bool = True


def default_data_dir() -> Path:
    if os.environ.get("JOULECODE_DATA"):
        return Path(os.environ["JOULECODE_DATA"])
    return Path.home() / ".joulecode" / "server"


def load_admin_token(data_dir: Path) -> tuple[str, bool]:
    """Admin token from $JOULECODE_ADMIN_TOKEN or `<data>/admin-token` (generated on first start).

    Returns (token, newly generated).
    """
    env = os.environ.get("JOULECODE_ADMIN_TOKEN")
    if env:
        return env, False
    path = data_dir / "admin-token"
    if path.is_file():
        return path.read_text(encoding="utf-8").strip(), False
    data_dir.mkdir(parents=True, exist_ok=True)
    token = new_token("gca")
    path.write_text(token + "\n", encoding="utf-8")
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    return token, True


def _bearer(authorization: str | None) -> str | None:
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return None


class NewProject(BaseModel):
    key: str
    name: str = ""


def create_app(config: ServerConfig) -> FastAPI:
    data_dir = Path(config.data_dir)
    store = Store(data_dir / "joulecode.db")
    worker = Worker(store, data_dir, config.processor)
    admin_hash = hash_token(config.admin_token)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if config.start_worker:
            worker.start()
        yield
        worker.stop()

    app = FastAPI(
        title="joule:code",
        version=__version__,
        description="Local platform: submit jobs, view findings and measurement results.",
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
        lifespan=lifespan,
    )
    app.state.store = store
    app.state.worker = worker
    app.state.config = config

    # -- Auth ------------------------------------------------------------------------
    def token(authorization: Annotated[str | None, Header()] = None) -> str | None:
        return _bearer(authorization)

    def require_admin(tok: Annotated[str | None, Depends(token)]) -> None:
        if not token_matches(tok, admin_hash):
            raise HTTPException(401, "Admin token missing or wrong")

    def project_or_404(key: str) -> Project:
        project = store.project(key)
        if not project:
            raise HTTPException(404, f"Project {key!r} does not exist")
        return project

    # -- API -------------------------------------------------------------------------
    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "version": __version__,
            "processor": config.processor.name,
            "modes": describe_modes(config.processor),
            "worker": "running" if worker.alive else "stopped",
            "current_job": worker.current,
            "queue": store.queue_counts(),
        }

    @app.get("/api/auth/check", dependencies=[Depends(require_admin)])
    def auth_check() -> dict[str, bool]:
        return {"admin": True}

    @app.get("/api/projects")
    def list_projects() -> list[dict[str, Any]]:
        return store.projects()

    @app.post("/api/projects", status_code=201, dependencies=[Depends(require_admin)])
    def create_project(body: NewProject) -> dict[str, Any]:
        try:
            project, tok = store.create_project(body.key.strip(), body.name)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        return {"project": project.public(), "token": tok}

    @app.get("/api/projects/{key}")
    def get_project(key: str) -> dict[str, Any]:
        project = project_or_404(key)
        gates = store.jobs(key, status=("done",), mode="gate", limit=100)
        trend = [
            {
                "id": j["id"],
                "created_at": j["created_at"],
                "branch": j["branch"],
                "commit_sha": j["commit_sha"],
                "change": j["summary"].get("change"),
                "ci95": j["summary"].get("ci95"),
                "verdict": j["summary"].get("verdict"),
                "gate": j["gate_status"],
            }
            for j in reversed(gates)
            if j["summary"] and j["summary"].get("change") is not None
        ]
        return {**project.public(), "branches": store.branches(key), "trend": trend}

    @app.post("/api/projects/{key}/token", dependencies=[Depends(require_admin)])
    def rotate_token(key: str) -> dict[str, str]:
        project_or_404(key)
        return {"token": store.rotate_token(key)}

    @app.get("/api/projects/{key}/jobs")
    def project_jobs(
        key: str, branch: str | None = None, mode: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        project_or_404(key)
        return store.jobs(key, branch=branch or None, mode=mode or None, limit=limit)

    @app.post("/api/projects/{key}/jobs", status_code=202)
    async def submit_job(
        key: str,
        request: Request,
        tok: Annotated[str | None, Depends(token)],
        archive: Annotated[UploadFile, File(description="repository as tar.gz")],
        mode: Annotated[str, Form()] = "review",
        branch: Annotated[str | None, Form()] = None,
        commit: Annotated[str | None, Form()] = None,
        base_ref: Annotated[str | None, Form()] = None,
        base_commit: Annotated[str | None, Form()] = None,
        message: Annotated[str | None, Form()] = None,
        options: Annotated[str, Form(description="JSON object")] = "{}",
        changed_files: Annotated[str, Form(description="JSON list")] = "[]",
        base_archive: Annotated[UploadFile | None, File(description="target branch as tar.gz")] = None,
        diff: Annotated[UploadFile | None, File(description="git diff -U0 against the target branch")] = None,
    ) -> dict[str, Any]:
        project_or_404(key)
        project = store.check_project_token(key, tok)
        if not project:
            raise HTTPException(401, "Project token missing or wrong")
        if mode not in MODES:
            raise HTTPException(400, f"mode must be one of {', '.join(MODES)}")
        try:
            opts = json.loads(options or "{}")
            changed = json.loads(changed_files or "[]")
            if not isinstance(opts, dict) or not isinstance(changed, list):
                raise ValueError
        except ValueError as e:
            raise HTTPException(400, "options must be a JSON object, changed_files a JSON list") from e
        if mode == "gate" and base_archive is None:
            raise HTTPException(400, "gate needs the state of the target branch (base_archive)")

        job_id = store.create_job(
            project,
            mode,
            branch=branch,
            commit_sha=commit,
            base_ref=base_ref,
            base_commit=base_commit,
            message=(message or "")[:500] or None,
            options=opts,
            changed_files=[str(f) for f in changed][:5000],
            has_base=base_archive is not None,
        )
        d = job_dir(data_dir, job_id)
        d.mkdir(parents=True, exist_ok=True)
        try:
            await _save_upload(archive, d / "head.tar.gz", config.max_upload_mb)
            if base_archive is not None:
                await _save_upload(base_archive, d / "base.tar.gz", config.max_upload_mb)
            if diff is not None:
                await _save_upload(diff, d / "changes.diff", config.max_upload_mb, allow_empty=True)
        except HTTPException as e:
            store.finish(job_id, "failed", error=str(e.detail))
            shutil.rmtree(d, ignore_errors=True)
            raise
        worker.notify()
        base = str(request.base_url).rstrip("/")
        return {
            "id": job_id,
            "status": "queued",
            "status_url": f"{base}/api/jobs/{job_id}",
            "ui_url": f"{base}/#/j/{job_id}",
        }

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str) -> dict[str, Any]:
        job = store.job(job_id)
        if not job:
            raise HTTPException(404, "Job not found")
        job["logs"] = store.logs(job_id)
        job["phases"] = config.processor.phases(job["mode"])
        return job

    @app.get("/api/jobs/{job_id}/sarif")
    def get_sarif(job_id: str) -> JSONResponse:
        job = store.job(job_id)
        if not job:
            raise HTTPException(404, "Job not found")
        if job["status"] != "done":
            raise HTTPException(409, f"Job is not finished yet ({job['status']})")
        return JSONResponse(
            to_sarif(job),
            media_type="application/sarif+json",
            headers={"Content-Disposition": f'attachment; filename="joulecode-{job_id}.sarif"'},
        )

    @app.get("/api/jobs/{job_id}/files/{path:path}")
    def get_file(job_id: str, path: str) -> FileResponse:
        root = (job_dir(data_dir, job_id) / "report").resolve()
        target = (root / path).resolve()
        if not store.job(job_id, with_result=False):
            raise HTTPException(404, "Job not found")
        if not target.is_relative_to(root) or not target.is_file():
            raise HTTPException(404, "File not found")
        media = {
            ".html": "text/html; charset=utf-8",
            ".md": "text/markdown; charset=utf-8",
            ".patch": "text/x-diff; charset=utf-8",
            ".json": "application/json",
        }.get(target.suffix, "application/octet-stream")
        # Reports contain code from the repository: show them in a separate, opaque origin
        # so that scripts in them cannot get at the admin token in the browser.
        headers = {"Content-Security-Policy": "sandbox allow-scripts", "X-Content-Type-Options": "nosniff"}
        return FileResponse(target, media_type=media, headers=headers)

    @app.post("/api/jobs/{job_id}/cancel")
    def cancel_job(job_id: str, tok: Annotated[str | None, Depends(token)]) -> dict[str, Any]:
        job = store.job(job_id, with_result=False)
        if not job:
            raise HTTPException(404, "Job not found")
        allowed = token_matches(tok, admin_hash) or store.check_project_token(job["project_key"], tok)
        if not allowed:
            raise HTTPException(401, "Admin or project token required")
        previous = store.request_cancel(job_id)
        return {"id": job_id, "previous_status": previous, "status": store.job(job_id)["status"]}

    @app.get("/api/queue")
    def queue() -> dict[str, Any]:
        return {
            "running": store.jobs(status=("running",), limit=10),
            "queued": store.jobs(status=("queued",), limit=100),
        }

    @app.exception_handler(Exception)
    async def unexpected(request: Request, exc: Exception):  # pragma: no cover - safety net
        log.exception("Error at %s", request.url.path)
        return JSONResponse({"detail": f"{type(exc).__name__}: {exc}"}, status_code=500)

    # -- Web interface ---------------------------------------------------------------
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    return app


def describe_modes(processor) -> dict[str, str]:
    """Which processing runs per mode (for the web interface and /api/health)."""
    if hasattr(processor, "describe"):
        return processor.describe()
    return dict.fromkeys(MODES, processor.name)


async def _save_upload(upload: UploadFile, target: Path, max_mb: int, *, allow_empty: bool = False) -> None:
    limit = max_mb * 1024 * 1024
    size = 0
    with target.open("wb") as out:
        while chunk := await upload.read(1024 * 1024):
            size += len(chunk)
            if size > limit:
                raise HTTPException(413, f"Archive larger than {max_mb} MB")
            out.write(chunk)
    if size == 0 and not allow_empty:
        raise HTTPException(400, f"{upload.filename or 'archive'} is empty")
