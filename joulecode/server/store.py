"""Persistence of the platform in SQLite.

One file, no additional service, survives restarts. Every call opens its
own connection – API threads and the worker thread thus share nothing but
the file (WAL mode, so that reading and writing do not block each other).

Tokens are only stored as SHA-256; the plain text is visible exactly once,
on creation.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id          INTEGER PRIMARY KEY,
    key         TEXT NOT NULL UNIQUE,
    name        TEXT NOT NULL,
    token_hash  TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
    id              TEXT PRIMARY KEY,
    project_id      INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    mode            TEXT NOT NULL,
    status          TEXT NOT NULL,              -- queued | running | done | failed | cancelled
    phase           TEXT,
    progress        REAL NOT NULL DEFAULT 0,
    branch          TEXT,
    commit_sha      TEXT,
    base_ref        TEXT,
    base_commit     TEXT,
    message         TEXT,
    options         TEXT NOT NULL DEFAULT '{}',
    changed_files   TEXT NOT NULL DEFAULT '[]',
    has_base        INTEGER NOT NULL DEFAULT 0,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL,
    started_at      TEXT,
    finished_at     TEXT,
    error           TEXT,
    gate_status     TEXT,                       -- passed | failed | skipped
    summary         TEXT,
    result          TEXT
);
CREATE INDEX IF NOT EXISTS jobs_project ON jobs(project_id, created_at);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status, created_at);
CREATE TABLE IF NOT EXISTS job_log (
    id       INTEGER PRIMARY KEY,
    job_id   TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    ts       TEXT NOT NULL,
    level    TEXT NOT NULL,
    message  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS job_log_job ON job_log(job_id, id);
"""

ACTIVE = ("queued", "running")
FINISHED = ("done", "failed", "cancelled")
PROJECT_KEY = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_token(prefix: str) -> str:
    return f"{prefix}_{secrets.token_urlsafe(24)}"


def token_matches(token: str | None, token_hash: str) -> bool:
    return bool(token) and hmac.compare_digest(hash_token(token), token_hash)


@dataclass
class Project:
    id: int
    key: str
    name: str
    created_at: str

    def public(self) -> dict[str, Any]:
        return {"key": self.key, "name": self.name, "created_at": self.created_at}


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript(SCHEMA)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA foreign_keys=ON")
        try:
            yield db
        finally:
            db.close()

    # -- Projects -----------------------------------------------------------------
    def create_project(self, key: str, name: str) -> tuple[Project, str]:
        """Create a project and return the token in plain text (only visible here)."""
        if not PROJECT_KEY.match(key):
            raise ValueError(
                "Project key: 1–64 characters, lower-case letters, digits, dot, hyphen and underscore"
            )
        token = new_token("gcp")
        created = now()
        with self.connect() as db:
            try:
                cur = db.execute(
                    "INSERT INTO projects(key, name, token_hash, created_at) VALUES (?, ?, ?, ?)",
                    (key, name.strip() or key, hash_token(token), created),
                )
            except sqlite3.IntegrityError as e:
                raise ValueError(f"Project {key!r} already exists") from e
        return Project(cur.lastrowid, key, name.strip() or key, created), token

    def rotate_token(self, key: str) -> str:
        token = new_token("gcp")
        with self.connect() as db:
            cur = db.execute("UPDATE projects SET token_hash=? WHERE key=?", (hash_token(token), key))
        if cur.rowcount == 0:
            raise KeyError(key)
        return token

    def project(self, key: str) -> Project | None:
        with self.connect() as db:
            row = db.execute("SELECT id, key, name, created_at FROM projects WHERE key=?", (key,)).fetchone()
        return Project(**dict(row)) if row else None

    def check_project_token(self, key: str, token: str | None) -> Project | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM projects WHERE key=?", (key,)).fetchone()
        if row and token_matches(token, row["token_hash"]):
            return Project(row["id"], row["key"], row["name"], row["created_at"])
        return None

    def projects(self) -> list[dict[str, Any]]:
        """All projects with key figures for the overview."""
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT p.id, p.key, p.name, p.created_at,
                       (SELECT COUNT(*) FROM jobs j WHERE j.project_id = p.id) AS job_count
                FROM projects p ORDER BY p.key
                """
            ).fetchall()
            out = []
            for r in rows:
                last = db.execute(
                    "SELECT * FROM jobs WHERE project_id=? ORDER BY created_at DESC, rowid DESC LIMIT 1",
                    (r["id"],),
                ).fetchone()
                last_gate = db.execute(
                    """SELECT * FROM jobs WHERE project_id=? AND mode='gate' AND status='done'
                       ORDER BY created_at DESC, rowid DESC LIMIT 1""",
                    (r["id"],),
                ).fetchone()
                out.append(
                    {
                        "key": r["key"],
                        "name": r["name"],
                        "created_at": r["created_at"],
                        "job_count": r["job_count"],
                        "last_job": _job_row(last) if last else None,
                        "last_gate": _job_row(last_gate) if last_gate else None,
                    }
                )
        return out

    # -- Jobs -----------------------------------------------------------------------
    def create_job(
        self,
        project: Project,
        mode: str,
        *,
        branch: str | None = None,
        commit_sha: str | None = None,
        base_ref: str | None = None,
        base_commit: str | None = None,
        message: str | None = None,
        options: dict | None = None,
        changed_files: list[str] | None = None,
        has_base: bool = False,
    ) -> str:
        job_id = uuid.uuid4().hex[:12]
        with self.connect() as db:
            db.execute(
                """INSERT INTO jobs(id, project_id, mode, status, phase, branch, commit_sha, base_ref,
                                    base_commit, message, options, changed_files, has_base, created_at)
                   VALUES (?, ?, ?, 'queued', 'queued', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    job_id,
                    project.id,
                    mode,
                    branch,
                    commit_sha,
                    base_ref,
                    base_commit,
                    message,
                    json.dumps(options or {}),
                    json.dumps(changed_files or []),
                    int(has_base),
                    now(),
                ),
            )
        self.log(job_id, "info", f"submitted ({mode})")
        return job_id

    def job(self, job_id: str, *, with_result: bool = True) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT j.*, p.key AS project_key, p.name AS project_name FROM jobs j "
                "JOIN projects p ON p.id = j.project_id WHERE j.id=?",
                (job_id,),
            ).fetchone()
            if not row:
                return None
            job = _job_row(row, with_result=with_result)
            job["queue_position"] = (
                db.execute(
                    "SELECT COUNT(*) FROM jobs WHERE status='queued' AND (created_at < ? OR "
                    "(created_at = ? AND rowid < (SELECT rowid FROM jobs WHERE id=?)))",
                    (row["created_at"], row["created_at"], job_id),
                ).fetchone()[0]
                + 1
                if row["status"] == "queued"
                else None
            )
        return job

    def jobs(
        self,
        project_key: str | None = None,
        *,
        status: tuple[str, ...] | None = None,
        branch: str | None = None,
        mode: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        sql = (
            "SELECT j.*, p.key AS project_key, p.name AS project_name FROM jobs j "
            "JOIN projects p ON p.id = j.project_id WHERE 1=1"
        )
        args: list[Any] = []
        if project_key:
            sql += " AND p.key=?"
            args.append(project_key)
        if status:
            sql += f" AND j.status IN ({','.join('?' * len(status))})"
            args.extend(status)
        if branch:
            sql += " AND j.branch=?"
            args.append(branch)
        if mode:
            sql += " AND j.mode=?"
            args.append(mode)
        order = "ASC" if status and set(status) <= set(ACTIVE) else "DESC"
        sql += f" ORDER BY j.created_at {order}, j.rowid {order} LIMIT ?"
        args.append(max(1, min(limit, 500)))
        with self.connect() as db:
            return [_job_row(r) for r in db.execute(sql, args).fetchall()]

    def branches(self, project_key: str) -> list[str]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT DISTINCT j.branch FROM jobs j JOIN projects p ON p.id=j.project_id "
                "WHERE p.key=? AND j.branch IS NOT NULL ORDER BY j.branch",
                (project_key,),
            ).fetchall()
        return [r[0] for r in rows]

    def claim_next(self) -> str | None:
        """Take the oldest queued job and set it to `running` (atomically)."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                row = db.execute(
                    "SELECT id FROM jobs WHERE status='queued' ORDER BY created_at, rowid LIMIT 1"
                ).fetchone()
                if row:
                    db.execute(
                        "UPDATE jobs SET status='running', started_at=?, phase='starting', progress=0 "
                        "WHERE id=?",
                        (now(), row["id"]),
                    )
                db.execute("COMMIT")
            except Exception:
                db.execute("ROLLBACK")
                raise
        return row["id"] if row else None

    def set_phase(self, job_id: str, phase: str, progress: float) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE jobs SET phase=?, progress=? WHERE id=?",
                (phase, max(0.0, min(1.0, progress)), job_id),
            )

    def finish(
        self,
        job_id: str,
        status: str,
        *,
        result: dict | None = None,
        summary: dict | None = None,
        gate_status: str | None = None,
        error: str | None = None,
    ) -> None:
        assert status in FINISHED
        with self.connect() as db:
            db.execute(
                """UPDATE jobs SET status=?, finished_at=?, result=?, summary=?, gate_status=?, error=?,
                                  progress=CASE WHEN ?='done' THEN 1 ELSE progress END,
                                  phase=CASE WHEN ?='done' THEN 'done' ELSE phase END
                   WHERE id=?""",
                (
                    status,
                    now(),
                    json.dumps(result, ensure_ascii=False) if result is not None else None,
                    json.dumps(summary, ensure_ascii=False) if summary is not None else None,
                    gate_status,
                    error,
                    status,
                    status,
                    job_id,
                ),
            )

    def request_cancel(self, job_id: str) -> str | None:
        """Queued jobs are cancelled immediately, running ones at the next phase change."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                db.execute("ROLLBACK")
                return None
            if row["status"] == "queued":
                db.execute(
                    "UPDATE jobs SET status='cancelled', finished_at=?, phase='cancelled' WHERE id=?",
                    (now(), job_id),
                )
            elif row["status"] == "running":
                db.execute("UPDATE jobs SET cancel_requested=1 WHERE id=?", (job_id,))
            db.execute("COMMIT")
        if row["status"] in ACTIVE:
            self.log(job_id, "warn", "cancellation requested")
        return row["status"]

    def cancel_requested(self, job_id: str) -> bool:
        with self.connect() as db:
            row = db.execute("SELECT cancel_requested FROM jobs WHERE id=?", (job_id,)).fetchone()
        return bool(row and row[0])

    def requeue_interrupted(self) -> list[str]:
        """After a restart: jobs that were running at shutdown go back into the queue."""
        with self.connect() as db:
            ids = [r[0] for r in db.execute("SELECT id FROM jobs WHERE status='running'").fetchall()]
            db.execute(
                "UPDATE jobs SET status='queued', phase='queued', progress=0, started_at=NULL "
                "WHERE status='running'"
            )
        for job_id in ids:
            self.log(job_id, "warn", "Server was stopped during processing – queued again")
        return ids

    def queue_counts(self) -> dict[str, int]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT status, COUNT(*) FROM jobs WHERE status IN ('queued','running') GROUP BY status"
            ).fetchall()
        counts = {"queued": 0, "running": 0}
        counts.update({r[0]: r[1] for r in rows})
        return counts

    # -- Log ------------------------------------------------------------------------
    def log(self, job_id: str, level: str, message: str) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT INTO job_log(job_id, ts, level, message) VALUES (?, ?, ?, ?)",
                (job_id, now(), level, message),
            )

    def logs(self, job_id: str) -> list[dict[str, str]]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT ts, level, message FROM job_log WHERE job_id=? ORDER BY id", (job_id,)
            ).fetchall()
        return [dict(r) for r in rows]


def _job_row(row: sqlite3.Row, *, with_result: bool = False) -> dict[str, Any]:
    d = dict(row)
    d.pop("project_id", None)
    d["options"] = json.loads(d.get("options") or "{}")
    d["changed_files"] = json.loads(d.get("changed_files") or "[]")
    d["summary"] = json.loads(d["summary"]) if d.get("summary") else None
    d["has_base"] = bool(d.get("has_base"))
    d["cancel_requested"] = bool(d.get("cancel_requested"))
    result = d.pop("result", None)
    if with_result:
        d["result"] = json.loads(result) if result else None
    return d
