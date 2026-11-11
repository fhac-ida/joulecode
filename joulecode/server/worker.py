"""Exactly one worker thread processes the queue.

A single worker is intentional, not a simplification: two measurements
at the same time distort each other, and the LLM phase and the measurement phase must
never overlap. The queue thus enforces centrally what the lock file
(`joulecode.cleanup.acquire_lock`) does for the CLI. As soon as real measurements
run, the worker additionally takes this lock so that a `joulecode measure` started
by hand in parallel does not interfere either.

Storage per job under `<data>/jobs/<id>/`:

    head.tar.gz, base.tar.gz   uploaded archives (kept for repetitions)
    changes.diff               `git diff -U0` against the target branch (optional)
    work/head, work/base       unpacked; deleted after the job
    result.json                result incl. gate
    report/                    full reports of gate/optimize (HTML, Markdown, JSON, patches)

After a restart, jobs that were running are queued again. A process that was stopped in the
middle of a job never reaches its clean-up, so `work/` and a partial `report/` are removed
before every attempt, and `recover` (if given) removes the measurement containers and the
metrion service the interrupted run left behind – before the first job is processed.
"""

from __future__ import annotations

import json
import logging
import shutil
import threading
import traceback
from collections.abc import Callable
from pathlib import Path

from joulecode.server import gate
from joulecode.server.archive import ArchiveError, safe_extract
from joulecode.server.processing import Cancelled, JobContext, Processor
from joulecode.server.store import Store

log = logging.getLogger("joulecode.server")


def job_dir(data_dir: Path, job_id: str) -> Path:
    return data_dir / "jobs" / job_id


class Worker:
    def __init__(
        self,
        store: Store,
        data_dir: Path,
        processor: Processor,
        poll_s: float = 2.0,
        recover: Callable[[], list[str]] | None = None,
    ):
        self.store = store
        self.data_dir = data_dir
        self.processor = processor
        self.poll_s = poll_s
        self.recover = recover
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.current: str | None = None
        self.requeued: list[str] = []

    # -- Life cycle -------------------------------------------------------------------
    def start(self) -> None:
        self.requeued = self.store.requeue_interrupted()
        for job_id in self.requeued:
            log.warning("Job %s was interrupted and has been queued again", job_id)
        self._thread = threading.Thread(target=self._loop, name="joulecode-worker", daemon=True)
        self._thread.start()

    def recover_interrupted(self) -> None:
        """Clean up after interrupted jobs (in the worker thread: stopping metrion can take minutes)."""
        if not self.requeued or not self.recover:
            return
        try:
            messages = self.recover()
        except Exception as exc:  # noqa: BLE001 - the worker must start anyway
            log.exception("Clean-up after the restart failed")
            messages = [f"Clean-up after the restart failed ({exc}) – run `joulecode cleanup`."]
        for msg in messages:
            log.warning(msg)
            for job_id in self.requeued:
                self.store.log(job_id, "warn", msg)
        self.requeued = []

    def stop(self, timeout: float = 10) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout)

    def notify(self) -> None:
        """New job queued – do not wait until the next poll."""
        self._wake.set()

    @property
    def alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def _loop(self) -> None:
        self.recover_interrupted()
        while not self._stop.is_set():
            try:
                worked = self.process_one()
            except Exception:  # the worker must never die
                log.exception("Unexpected error in the worker")
                worked = False
            if not worked:
                self._wake.wait(self.poll_s)
                self._wake.clear()

    # -- one job ----------------------------------------------------------------------
    def process_one(self) -> bool:
        """Process the next queued job. False if none is waiting."""
        job_id = self.store.claim_next()
        if not job_id:
            return False
        self.current = job_id
        try:
            self._process(job_id)
        finally:
            self.current = None
        return True

    def _process(self, job_id: str) -> None:
        job = self.store.job(job_id, with_result=False)
        assert job is not None
        d = job_dir(self.data_dir, job_id)
        work = d / "work"
        self.store.log(job_id, "info", f"started ({self.processor.name})")
        # leftovers of an attempt interrupted by a restart (its `finally` never ran)
        stale = [p for p in (work, d / "report") if p.exists()]
        for p in stale:
            shutil.rmtree(p, ignore_errors=True)
        if stale:
            self.store.log(job_id, "info", "Files of the interrupted attempt removed")
        try:
            head = work / "head"
            n = safe_extract(d / "head.tar.gz", head)
            base = None
            if job["has_base"]:
                base = work / "base"
                safe_extract(d / "base.tar.gz", base)
            self.store.log(job_id, "info", f"{n} files unpacked")

            ctx = JobContext(
                job=job,
                workdir=work,
                head_dir=head,
                base_dir=base,
                report_phase=lambda name, p: self.store.set_phase(job_id, name, p),
                log=lambda level, msg: self.store.log(job_id, level, msg),
                is_cancelled=lambda: self.store.cancel_requested(job_id),
                phases=self.processor.phases(job["mode"]),
                diff=(d / "changes.diff").read_text(encoding="utf-8", errors="replace")
                if (d / "changes.diff").is_file()
                else None,
                report_dir=d / "report",
            )
            result = self.processor.run(ctx)
            result["gate"] = gate.evaluate(job, result)
            result.setdefault("warnings", []).extend(result["gate"].pop("warnings"))
            summary = dict(result.get("summary") or {})
            summary["gate"] = result["gate"]["status"]
            (d / "result.json").write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
            self.store.finish(
                job_id, "done", result=result, summary=summary, gate_status=result["gate"]["status"]
            )
            self.store.log(job_id, "info", f"done – gate: {result['gate']['status']}")
        except Cancelled:
            self.store.finish(job_id, "cancelled")
            self.store.log(job_id, "warn", "cancelled")
        except ArchiveError as e:
            self.store.finish(job_id, "failed", error=f"Archive rejected: {e}")
            self.store.log(job_id, "error", f"Archive rejected: {e}")
        except Exception as e:
            log.exception("Job %s failed", job_id)
            self.store.finish(job_id, "failed", error=f"{type(e).__name__}: {e}")
            self.store.log(job_id, "error", traceback.format_exc(limit=5))
        finally:
            shutil.rmtree(work, ignore_errors=True)
