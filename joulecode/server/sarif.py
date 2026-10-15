"""Result → SARIF 2.1.0 so that findings appear directly in the code review.

GitHub code scanning (`github/codeql-action/upload-sarif`) shows SARIF results as
annotations on the lines in the pull request; many other tools import SARIF as well.
`joulecode submit --sarif joulecode.sarif` writes the file, `GET /api/jobs/{id}/sarif`
returns it from the server.

Mapping: major → error, minor → warning, info → note. Every rule is listed once under
`tool.driver.rules`; evidence level, category, function and “in diff” are stored in
`properties` so that analyses can filter by them.
"""

from __future__ import annotations

from typing import Any

from joulecode import __version__

LEVEL = {"major": "error", "minor": "warning", "info": "note"}
SCHEMA = "https://json.schemastore.org/sarif-2.1.0.json"


def to_sarif(job: dict[str, Any]) -> dict[str, Any]:
    result = job.get("result") or {}
    findings = result.get("findings") or []
    rules: dict[str, dict[str, Any]] = {}
    results = []
    for f in findings:
        rule_id = f"{f['tool']}/{f['rule']}"
        if rule_id not in rules:
            rules[rule_id] = {
                "id": rule_id,
                "name": f["rule"],
                "shortDescription": {"text": f["rule"]},
                "fullDescription": {"text": f.get("why") or f["message"]},
                "defaultConfiguration": {"level": LEVEL.get(f["severity"], "note")},
                "properties": {"tool": f["tool"], "category": f.get("category", "other")},
            }
        message = f["message"]
        if f.get("why"):
            message += f" — {f['why']}"
        if f.get("evidence_detail"):
            message += f" (evidence: {f['evidence_detail']})"
        entry: dict[str, Any] = {
            "ruleId": rule_id,
            "level": LEVEL.get(f["severity"], "note"),
            "message": {"text": message},
            "locations": [
                {
                    "physicalLocation": {
                        "artifactLocation": {"uri": f["file"], "uriBaseId": "%SRCROOT%"},
                        "region": {"startLine": max(1, int(f.get("line") or 1))},
                    },
                    **(
                        {"logicalLocations": [{"fullyQualifiedName": f["function"], "kind": "function"}]}
                        if f.get("function")
                        else {}
                    ),
                }
            ],
            "partialFingerprints": {
                # stable across line shifts: file + function + rule
                "joulecode/v1": f"{f['file']}|{f.get('function') or ''}|{rule_id}"
            },
            "properties": {
                "evidence": f.get("evidence"),
                "category": f.get("category"),
                "inDiff": bool(f.get("in_diff")),
                "joulecodeJob": job.get("id"),
            },
        }
        results.append(entry)
    return {
        "$schema": SCHEMA,
        "version": "2.1.0",
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": "joule:code",
                        "version": __version__,
                        "rules": list(rules.values()),
                    }
                },
                "automationDetails": {"id": f"joulecode/{job.get('mode', 'review')}/"},
                "results": results,
                "properties": {
                    "dummy": bool(result.get("dummy")),
                    "gate": (result.get("gate") or {}).get("status"),
                },
            }
        ],
    }
