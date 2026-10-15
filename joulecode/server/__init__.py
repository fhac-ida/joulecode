"""joule:code platform: a local server in the style of SonarQube.

joulecode serve    API + web interface + job queue with exactly one worker
joulecode submit   client for CI: pack the repository, upload it, wait for the result

Structure:

    store.py       SQLite (projects, jobs, job log) – standard library only
    archive.py     pack a repository (client) and unpack it safely (server)
    processing.py  processing of a job behind `Processor`; `DummyProcessor` for testing
    worker.py      one worker thread: measurements must never run in parallel
    gate.py        quality gate from the result and the job options
    app.py         FastAPI application (only with `pip install joulecode[server]`)
    static/        web interface (no build step, no external dependencies)

This module deliberately imports nothing from `app.py` so that `joulecode submit`
works without FastAPI.
"""

RESULT_SCHEMA = "joulecode-platform-result/1"
MODES = ("review", "gate", "optimize")
