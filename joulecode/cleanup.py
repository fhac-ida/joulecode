"""Cleaning up after aborted runs: measurement containers, images, temp directories, metrion service, old results.

Everything joule:code creates in Docker carries the label `joulecode=1`; the
Ollama container is not part of it and stays untouched. During a run
there is a lock file with the PID of the process – as long as it lives,
`cleanup` does not clean up.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from joulecode.container import LABEL

LOCK = Path(os.environ.get("XDG_RUNTIME_DIR", tempfile.gettempdir())) / f"joulecode-{os.getuid()}.lock"
TEMP_PREFIXES = ("joulecode-", "joulecode-metrion-")
LABELS = (LABEL,)


# -- Lock file for running measurements ----------------------------------------------
def acquire_lock(command: str) -> None:
    LOCK.write_text(f"{os.getpid()} {command}")


def release_lock() -> None:
    try:
        if LOCK.read_text().split()[0] == str(os.getpid()):
            LOCK.unlink()
    except (OSError, IndexError):
        pass


def active_run() -> str | None:
    """Description of the running joule:code process or None."""
    try:
        pid, _, command = LOCK.read_text().partition(" ")
        os.kill(int(pid), 0)
    except (OSError, ValueError):
        return None
    return f"joulecode {command} (PID {pid})"


# -- Cleaning up ----------------------------------------------------------------------------
@dataclass
class CleanupReport:
    containers: list[str] = field(default_factory=list)
    images: list[str] = field(default_factory=list)
    temp_dirs: list[str] = field(default_factory=list)
    run_dirs: list[str] = field(default_factory=list)
    freed_mb: float = 0.0
    metrion_stopped: bool = False
    notes: list[str] = field(default_factory=list)


def _size_mb(path: Path) -> float:
    if path.is_file():
        return path.stat().st_size / 1e6
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file() and not f.is_symlink()) / 1e6


def cleanup(
    *,
    client=None,
    images: bool = False,
    runs_older_than_days: float | None = None,
    runs_dir: Path = Path("joulecode-runs"),
    temp_root: Path | None = None,
    stop_metrion: Callable[[], bool] | None = None,
    dry_run: bool = False,
) -> CleanupReport:
    report = CleanupReport()
    temp_root = temp_root or Path(tempfile.gettempdir())

    if client is None:
        import docker

        client = docker.from_env()

    # 1. Measurement containers (running or stopped)
    found = (c for lbl in LABELS for c in client.containers.list(all=True, filters={"label": f"{lbl}=1"}))
    containers = list({c.short_id: c for c in found}.values())
    for c in containers:
        report.containers.append(f"{c.short_id} ({c.status})")
        if not dry_run:
            c.remove(force=True)

    # 2. Images of the variants (rebuilt from the cache on the next run)
    if images:
        found = (i for lbl in LABELS for i in client.images.list(filters={"label": f"{lbl}=1"}))
        for img in {i.id: i for i in found}.values():
            report.images.append(", ".join(img.tags) or img.short_id)
            report.freed_mb += (img.attrs.get("Size") or 0) / 1e6
            if not dry_run:
                client.images.remove(img.id, force=True)
    if not dry_run:
        # Unnamed intermediate states without a tag that only come from joule:code
        for lbl in LABELS:
            client.images.prune(filters={"dangling": True, "label": f"{lbl}=1"})

    # 3. Temp directories of aborted runs (own ones only)
    for path in sorted(temp_root.iterdir()) if temp_root.is_dir() else []:
        if path.is_dir() and path.name.startswith(TEMP_PREFIXES) and path.stat().st_uid == os.getuid():
            report.temp_dirs.append(str(path))
            report.freed_mb += _size_mb(path)
            if not dry_run:
                shutil.rmtree(path, ignore_errors=True)

    # 4. Old result directories (also contain the archived metrion databases)
    if runs_older_than_days is not None and runs_dir.is_dir():
        limit = time.time() - runs_older_than_days * 86400
        for path in sorted(runs_dir.iterdir()):
            if path.is_dir() and path.stat().st_mtime < limit:
                report.run_dirs.append(str(path))
                report.freed_mb += _size_mb(path)
                if not dry_run:
                    shutil.rmtree(path, ignore_errors=True)

    # 5. metrion service, if it is still running after an abort
    if stop_metrion is not None:
        report.metrion_stopped = stop_metrion() if not dry_run else False

    return report


def render(report: CleanupReport, dry_run: bool) -> str:
    verb = "to remove" if dry_run else "removed"
    lines = []
    for label, items in (
        ("Measurement containers", report.containers),
        ("Images", report.images),
        ("Temp directories", report.temp_dirs),
        ("Result directories", report.run_dirs),
    ):
        lines.append(f"{label} {verb}: {len(items)}")
        lines.extend(f"  {x}" for x in items[:20])
        if len(items) > 20:
            lines.append(f"  … and {len(items) - 20} more")
    if report.metrion_stopped:
        lines.append("metrion service stopped")
    if report.freed_mb:
        lines.append(f"Disk space {'to be freed' if dry_run else 'freed'}: approx. {report.freed_mb:,.0f} MB")
    lines.extend(report.notes)
    return "\n".join(lines)
