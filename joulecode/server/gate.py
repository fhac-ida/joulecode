"""Quality gate: decides from the result and the job options whether CI turns red.

Conditions (job options, set by `joulecode submit`):

    max_regression  (gate/optimize) fraction, e.g. 0.05. Red if the measurement
                    yields “worse” AND the whole 95% CI lies above this threshold
                    – as with “better”, the median alone never decides.
                    Significantly worse, but below the threshold: warning.
    fail_on         none | minor | major. Red if there is a finding of at least this
                    severity – with a known diff only in the changed code (in_diff).

    Tests (PR)      (gate) red if the PR's tests fail – then nothing is measured.

Without conditions the gate is `skipped` (exit code 0).
"""

from __future__ import annotations

from typing import Any

SEVERITY_RANK = {"info": 0, "minor": 1, "major": 2}
DEFAULT_MAX_REGRESSION = 0.05


def _pct(x: float) -> str:
    return f"{x * 100:+.1f}%"


def evaluate(job: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    options = job.get("options") or {}
    conditions: list[dict[str, Any]] = []
    warnings: list[str] = []

    m = result.get("measurement")
    if m and job["mode"] in ("gate", "optimize"):
        threshold = float(options.get("max_regression", DEFAULT_MAX_REGRESSION))
        lo, hi = m["ci95"]
        worse = m["verdict"] == "worse"
        failed = job["mode"] == "gate" and worse and lo > threshold
        if worse and not failed and job["mode"] == "gate":
            warnings.append(
                f"Significantly more energy ({_pct(m['change'])}), but the CI reaches below the "
                f"threshold of {_pct(threshold)} – the gate stays green."
            )
        if job["mode"] == "gate":
            conditions.append(
                {
                    "name": "Energy regression",
                    "threshold": threshold,
                    "actual": m["change"],
                    "ci95": [lo, hi],
                    "passed": not failed,
                    "text": f"change {_pct(m['change'])} (CI {_pct(lo)} … {_pct(hi)}), threshold {_pct(threshold)}",
                }
            )

    pr_tests = [t for t in result.get("tests") or [] if t.get("variant") == "PR"]
    if job["mode"] == "gate" and pr_tests:
        ok = all(t["ok"] for t in pr_tests)
        conditions.append(
            {
                "name": "Tests (PR)",
                "threshold": None,
                "actual": ok,
                "passed": ok,
                "text": "passed" if ok else "failed – nothing is measured without green tests",
            }
        )

    fail_on = options.get("fail_on", "none")
    if fail_on in SEVERITY_RANK:
        findings = result.get("findings") or []
        if job.get("changed_files"):
            findings = [f for f in findings if f.get("in_diff")]
        hits = [f for f in findings if SEVERITY_RANK.get(f["severity"], 0) >= SEVERITY_RANK[fail_on]]
        conditions.append(
            {
                "name": "Findings",
                "threshold": fail_on,
                "actual": len(hits),
                "passed": not hits,
                "text": f"{len(hits)} finding(s) of severity “{fail_on}” or higher"
                + (" in the changed code" if job.get("changed_files") else ""),
            }
        )

    status = "skipped" if not conditions else "passed" if all(c["passed"] for c in conditions) else "failed"
    return {"status": status, "conditions": conditions, "warnings": warnings}
