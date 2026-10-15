"""`joulecode submit`: send a repository to a joule:code server – for CI and locally.

    joulecode submit                                   # review of the current directory
    joulecode submit --mode gate --base origin/main    # energy regression against the target branch

Flow: pack the repository (see `joulecode.server.archive`) → upload → wait for the
result → print a summary, optionally save it as JSON and, in
GitHub Actions, write it to the job summary.

Exit codes: 0 = gate passed or no conditions (or --no-wait),
1 = gate failed, 2 = error (connection, job failed, timeout).

Configuration via options or environment variables (handy for CI secrets):
JOULECODE_SERVER, JOULECODE_PROJECT, JOULECODE_TOKEN.
Branch, commit and target branch are determined from Git or the variables of GitHub
Actions and GitLab CI.

Only needs httpx (a dependency of joule:code anyway), no FastAPI.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from joulecode.server import MODES
from joulecode.server.archive import ArchiveError, git, is_git_repo, pack_commit, pack_worktree

DEFAULT_SERVER = "http://127.0.0.1:8765"
EXIT_OK, EXIT_GATE_FAILED, EXIT_ERROR = 0, 1, 2
# Measurements wait in the queue and take time: gate minutes, optimize hours.
DEFAULT_TIMEOUT = {"review": 600, "gate": 7200, "optimize": 6 * 3600}
FINISHED = ("done", "failed", "cancelled")


class SubmitError(Exception):
    pass


@dataclass
class GitInfo:
    branch: str | None = None
    commit: str | None = None
    message: str | None = None
    base_ref: str | None = None
    base_commit: str | None = None
    changed_files: list[str] = field(default_factory=list)
    diff: str | None = None  # git diff -U0 against the target branch (lines of the changes)
    dirty: bool = False


def default_base_ref() -> str | None:
    """Target branch of a pull/merge request from the CI variables."""
    for var in ("GITHUB_BASE_REF", "CI_MERGE_REQUEST_TARGET_BRANCH_NAME"):
        if os.environ.get(var):
            return f"origin/{os.environ[var]}"
    return None


def git_info(repo: Path, base_ref: str | None) -> GitInfo:
    info = GitInfo(base_ref=base_ref)
    if not is_git_repo(repo):
        if base_ref:
            raise SubmitError(f"--base {base_ref}: {repo} is not a Git repository")
        return info
    info.commit = git(repo, "rev-parse", "HEAD", check=False) or None
    info.message = git(repo, "log", "-1", "--format=%s", check=False) or None
    info.dirty = bool(git(repo, "status", "--porcelain", check=False))
    branch = (
        os.environ.get("GITHUB_HEAD_REF")
        or os.environ.get("CI_MERGE_REQUEST_SOURCE_BRANCH_NAME")
        or os.environ.get("CI_COMMIT_REF_NAME")
        or os.environ.get("GITHUB_REF_NAME")
        or git(repo, "rev-parse", "--abbrev-ref", "HEAD", check=False)
    )
    info.branch = None if branch in ("", "HEAD") else branch
    if base_ref:
        try:
            info.base_commit = git(repo, "merge-base", "HEAD", base_ref)
        except ArchiveError as e:
            raise SubmitError(
                f"Common ancestor of HEAD and {base_ref} not found ({e}). "
                "Check out the whole history in CI (GitHub: fetch-depth: 0, GitLab: GIT_DEPTH: 0)."
            ) from e
        # against the working tree, i.e. including uncommitted changes; --relative so that the
        # paths also match the uploaded archive for a subdirectory of a monorepo
        names = git(repo, "diff", "--relative", "--name-only", info.base_commit)
        untracked = git(repo, "ls-files", "--others", "--exclude-standard")
        info.changed_files = sorted({f for f in (names + "\n" + untracked).splitlines() if f})
        info.diff = git(repo, "diff", "--relative", "-U0", "--no-color", "--no-ext-diff", info.base_commit)
    return info


def _client(server: str, token: str | None, client: httpx.Client | None) -> httpx.Client:
    if client is not None:
        if token:
            client.headers["Authorization"] = f"Bearer {token}"
        return client
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return httpx.Client(base_url=server.rstrip("/"), headers=headers, timeout=httpx.Timeout(30, write=300))


def _error_text(r: httpx.Response) -> str:
    try:
        detail = r.json().get("detail")
    except ValueError:
        detail = r.text[:300]
    return f"HTTP {r.status_code}: {detail}"


def submit(
    repo: Path,
    *,
    server: str,
    project: str,
    token: str,
    mode: str = "review",
    base_ref: str | None = None,
    options: dict[str, Any] | None = None,
    client: httpx.Client | None = None,
    out=sys.stdout,
) -> dict[str, Any]:
    """Pack and upload; return the server's answer (id, status_url, ui_url)."""
    repo = repo.resolve()
    if not repo.is_dir():
        raise SubmitError(f"{repo} is not a directory")
    if mode == "gate" and not base_ref:
        raise SubmitError("gate compares with the target branch: specify --base (e.g. --base origin/main)")
    info = git_info(repo, base_ref)

    head = pack_worktree(repo)
    files = {"archive": ("head.tar.gz", head, "application/gzip")}
    if info.diff is not None:
        files["diff"] = ("changes.diff", info.diff.encode("utf-8", "surrogateescape"), "text/x-diff")
    if mode == "gate":
        files["base_archive"] = ("base.tar.gz", pack_commit(repo, info.base_commit), "application/gzip")
    opts = dict(options or {})
    if info.dirty:
        opts["dirty"] = True
    data = {
        "mode": mode,
        "options": json.dumps(opts),
        "changed_files": json.dumps(info.changed_files),
    }
    meta = {
        "branch": info.branch,
        "commit": info.commit,
        "base_ref": info.base_ref,
        "base_commit": info.base_commit,
        "message": info.message,
    }
    data.update({k: v for k, v in meta.items() if v})

    size = sum(len(f[1]) for name, f in files.items() if name != "diff")
    print(
        f"joule:code: {mode} for {project}"
        + (f" – {info.branch}" if info.branch else "")
        + (f" @ {info.commit[:10]}" if info.commit else "")
        + (" (with local changes)" if info.dirty else "")
        + (f", base {info.base_ref} @ {info.base_commit[:10]}" if info.base_commit else "")
        + f" – {size / 1024:.0f} KB",
        file=out,
    )
    if info.changed_files:
        print(f"  {len(info.changed_files)} changed file(s) relative to {info.base_ref}", file=out)

    c = _client(server, token, client)
    try:
        r = c.post(f"/api/projects/{project}/jobs", data=data, files=files)
    except httpx.HTTPError as e:
        raise SubmitError(f"Server {server} not reachable: {e}") from e
    if r.status_code != 202:
        raise SubmitError(f"Submission failed – {_error_text(r)}")
    return r.json()


def wait(
    job_id: str,
    *,
    server: str,
    token: str | None = None,
    timeout_s: float = 3600,
    poll_s: float = 3.0,
    client: httpx.Client | None = None,
    out=sys.stdout,
) -> dict[str, Any]:
    c = _client(server, token, client)
    deadline = time.monotonic() + timeout_s
    last = None
    delay = 0.2  # check quickly first (review is often done after seconds), then less often
    while True:
        try:
            r = c.get(f"/api/jobs/{job_id}")
        except httpx.HTTPError as e:
            raise SubmitError(f"Server not reachable: {e}") from e
        if r.status_code != 200:
            raise SubmitError(f"Querying the status failed – {_error_text(r)}")
        job = r.json()
        state = (job["status"], job.get("phase"), job.get("queue_position"))
        if state != last:
            where = f" (position {job['queue_position']} in the queue)" if job.get("queue_position") else ""
            print(f"  [{job['status']}] {job.get('phase') or ''}{where}", file=out)
            last = state
        if job["status"] in FINISHED:
            return job
        if time.monotonic() > deadline:
            raise SubmitError(f"Timeout after {timeout_s:.0f} s – job {job_id} keeps running")
        time.sleep(delay)
        delay = min(poll_s, delay * 2)


# -- Output ----------------------------------------------------------------------------
def _pct(x: float) -> str:
    return f"{x * 100:+.1f}%"


def summary_lines(job: dict[str, Any]) -> list[str]:
    result = job.get("result") or {}
    lines = []
    if result.get("dummy"):
        lines.append("Note: dummy result – processing is not connected yet.")
    lines.extend(
        f"Tests {t['variant']}: {'passed' if t['ok'] else 'FAILED'}" for t in result.get("tests") or []
    )
    m = result.get("measurement")
    if m:
        lo, hi = m["ci95"]
        what = "Energy" if m.get("is_energy", True) else (m.get("quantity") or "Metric")
        best = f" – best candidate {result['best']}" if result.get("best") else ""
        lines.append(
            f"{what}: {_pct(m['change'])} (95% CI {_pct(lo)} … {_pct(hi)}) – {m['verdict']} "
            f"[{m['backend']}, n={m['baseline']['n']}]{best}"
        )
    cands = result.get("candidates") or []
    if cands and job.get("mode") == "optimize":
        lines.append(f"{len(cands)} candidate(s):")
        lines.extend(
            f"  {'★' if c.get('pareto') else ' '} {c['id']:<8} {c.get('status', ''):<13}"
            + (f" {_pct(c['change'])} {c['verdict']}" if c.get("change") is not None else "")
            + f" – {c.get('summary', '')}"
            for c in cands[:20]
        )
    findings = result.get("findings") or []
    summary = result.get("summary") or {}
    where = " in the changed code" if summary.get("scope") == "changed" else ""
    if summary.get("changed_functions") is not None and summary.get("scope") == "changed":
        lines.append(f"{summary['changed_functions']} changed function(s) checked.")
    if findings or job.get("mode") != "optimize":
        lines.append(f"{len(findings)} finding(s){where}" + (":" if findings else "."))
    if findings:
        lines.extend(
            f"  {f['severity']:<5} {f['file']}:{f['line']}"
            + (f" {f['function']}" if f.get("function") else "")
            + f" – {f['rule']}: {f['message']} [{f['evidence']}]"
            + (
                " (changed code)"
                if f.get("in_diff") and summary.get("scope") == "all" and job.get("base_ref")
                else ""
            )
            for f in findings[:20]
        )
        if len(findings) > 20:
            lines.append(f"  … and {len(findings) - 20} more")
    if summary.get("findings_outside_diff"):
        lines.append(f"  ({summary['findings_outside_diff']} more in the remaining code – show with --all)")
    lines.extend(f"Warning: {w}" for w in result.get("warnings") or [])
    g = result.get("gate") or {}
    lines.extend(
        f"Gate {'✔' if c['passed'] else '✘'} {c['name']}: {c['text']}" for c in g.get("conditions", [])
    )
    if g:
        lines.append(f"Gate: {g['status']}")
    return lines


def _md_cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def markdown_summary(job: dict[str, Any]) -> str:
    """Short summary for $GITHUB_STEP_SUMMARY or a PR comment."""
    result = job.get("result") or {}
    g = result.get("gate") or {}
    icon = {"passed": "✅", "failed": "❌", "skipped": "➖"}.get(g.get("status"), "⚠️")
    out = [f"### {icon} joule:code {job['mode']} – Gate: {g.get('status', job['status'])}", ""]
    if result.get("dummy"):
        out += ["> Dummy result – processing is not connected yet.", ""]
    failed_tests = [t["variant"] for t in result.get("tests") or [] if not t["ok"]]
    if failed_tests:
        out += [f"Tests failed: {', '.join(failed_tests)} – nothing was measured.", ""]
    m = result.get("measurement")
    if m:
        lo, hi = m["ci95"]
        out += [
            "| Metric | Original | New | Change | 95% CI | Verdict |",
            "|---|---|---|---|---|---|",
            f"| {m['quantity']} ({m['unit']}) | {m['baseline']['median']:.4g} | {m['candidate']['median']:.4g} | "
            f"{_pct(m['change'])} | {_pct(lo)} … {_pct(hi)} | {m['verdict']} |",
            "",
        ]
    findings = result.get("findings") or []
    if findings:
        out += ["| Severity | Location | Rule | Finding | Evidence |", "|---|---|---|---|---|"]
        out += [
            f"| {f['severity']} | `{f['file']}:{f['line']}` | {f['rule']} | {_md_cell(f['message'])} | {f['evidence']} |"
            for f in findings[:30]
        ]
        out.append("")
    if job.get("ui_url"):
        out.append(f"[Details in joule:code]({job['ui_url']})")
    return "\n".join(out) + "\n"


def exit_code(job: dict[str, Any]) -> int:
    if job["status"] != "done":
        return EXIT_ERROR
    gate = (job.get("result") or {}).get("gate") or {}
    return EXIT_GATE_FAILED if gate.get("status") == "failed" else EXIT_OK


# -- CLI ------------------------------------------------------------------------------
def add_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument("repo", nargs="?", default=".", help="repository (default: current directory)")
    p.add_argument("--server", default=os.environ.get("JOULECODE_SERVER", DEFAULT_SERVER))
    p.add_argument("--project", default=os.environ.get("JOULECODE_PROJECT"), help="project key")
    p.add_argument("--token", default=os.environ.get("JOULECODE_TOKEN"), help="project token")
    p.add_argument("--mode", choices=MODES, default="review")
    p.add_argument(
        "--base",
        default=default_base_ref(),
        help="target branch, e.g. origin/main (gate: mandatory; review: only consider changed files)",
    )
    p.add_argument(
        "--max-regression",
        type=float,
        default=5.0,
        metavar="PERCENT",
        help="gate: red if the whole CI is more than PERCENT worse (default 5)",
    )
    p.add_argument(
        "--fail-on",
        choices=("none", "info", "minor", "major"),
        default="none",
        help="red for findings of this severity or higher (default none)",
    )
    p.add_argument("--no-wait", action="store_true", help="only submit, do not wait for the result")
    p.add_argument(
        "--timeout",
        type=float,
        help="seconds submit waits (default: review 600, gate 7200, optimize 21600); "
        "the job keeps running afterwards",
    )
    p.add_argument(
        "--all",
        action="store_true",
        help="review with --base: also report findings outside the changed code",
    )
    p.add_argument("--output", help="write the job incl. result as JSON to this file")
    p.add_argument(
        "--sarif", help="write findings as SARIF 2.1.0 to this file (e.g. for GitHub code scanning)"
    )


def cmd_submit(args: argparse.Namespace, client: httpx.Client | None = None, out=sys.stdout) -> int:
    if not args.project or not args.token:
        print("Specify --project and --token (or JOULECODE_PROJECT/JOULECODE_TOKEN).", file=sys.stderr)
        return EXIT_ERROR
    options: dict[str, Any] = {"fail_on": args.fail_on}
    if getattr(args, "all", False):
        options["scope"] = "all"
    if args.mode == "gate":
        options["max_regression"] = args.max_regression / 100
    try:
        sub = submit(
            Path(args.repo),
            server=args.server,
            project=args.project,
            token=args.token,
            mode=args.mode,
            base_ref=args.base,
            options=options,
            client=client,
            out=out,
        )
        print(f"  Job {sub['id']}: {sub['ui_url']}", file=out)
        if args.no_wait:
            return EXIT_OK
        timeout = args.timeout or DEFAULT_TIMEOUT[args.mode]
        job = wait(sub["id"], server=args.server, timeout_s=timeout, client=client, out=out)
    except (SubmitError, ArchiveError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return EXIT_ERROR

    job["ui_url"] = sub["ui_url"]
    if job["status"] != "done":
        print(f"Job {job['status']}: {job.get('error') or ''}", file=out)
    for line in summary_lines(job):
        print(line, file=out)
    if args.output:
        Path(args.output).write_text(json.dumps(job, indent=2, ensure_ascii=False), encoding="utf-8")
    if getattr(args, "sarif", None) and job["status"] == "done":
        from joulecode.server.sarif import to_sarif

        Path(args.sarif).write_text(json.dumps(to_sarif(job), indent=2, ensure_ascii=False), encoding="utf-8")
    step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary and job["status"] == "done":
        with open(step_summary, "a", encoding="utf-8") as f:
            f.write(markdown_summary(job))
    return exit_code(job)
