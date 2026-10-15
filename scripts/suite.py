#!/usr/bin/env python3
"""Test suite for joule:code: run several examples with expectations.

    python scripts/suite.py check                 # check the examples and calibrate the benchmarks
    python scripts/suite.py run                   # all examples through `joulecode optimize`
    python scripts/suite.py run --only editdist --repeat 3 -- --backend cgroup --repetitions 5
    python scripts/suite.py evaluate suite-runs/20261007-2200   # regenerate the summary

`check` needs neither an LLM nor a measuring device: for every example it checks whether the original and
the reference patch pass the visible and the holdout tests, and how long a
benchmark run takes (with a suggestion for BENCH_ROUNDS).

`run` starts `joulecode optimize` for every example as a separate process (arguments after
`--` are passed through), then checks every candidate that passed the test gate
with the holdout tests and, after every run, writes
`summary.md`/`summary.json` to the suite directory. Aborting with Ctrl+C keeps
everything finished up to that point.

What the expectations mean is described in examples/suite/suite.yml.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from joulecode.config import load_config  # noqa: E402
from joulecode.container import ExecResult, image_tag  # noqa: E402
from joulecode.variants import IGNORE  # noqa: E402

DEFAULT_SUITE = ROOT / "examples" / "suite" / "suite.yml"
# Default for Python examples; other languages set holdout_dir/holdout_command in suite.yml.
HOLDOUT_DIR = "_holdout"
HOLDOUT_COMMAND = f"pytest -q -p no:cacheprovider {HOLDOUT_DIR}"
EXPECTATIONS = ("improvement", "no_improvement", "no_hotspots")
# Candidates with these statuses have passed the visible tests.
GATE_PASSED = {"ready", "measured", "vetoed", "measurement_failed"}
# Options of `joulecode optimize` that `joulecode measure` understands as well (for --reference).
MEASURE_OPTIONS = {
    "--backend": True,
    "--metrion-results": True,
    "--rapl-idle": True,
    "--repetitions": True,
    "--min-repetitions": True,
    "--fixed": False,
    "--warmup": True,
    "--cooldown": True,
    "--seed": True,
    "--env": True,
    "--force": False,
}


# -- Suite description -------------------------------------------------------------
@dataclass
class Example:
    name: str
    repo: Path
    expect: str
    category: str = ""
    description: str = ""
    min_gain: float = 0.10
    tolerance: float = 0.05
    hotspots: list[str] = field(default_factory=list)
    holdout: Path | None = None
    reference: Path | None = None
    # Where the holdout tests are put in the copy (relative to the repository) and how they run.
    # For Java e.g. under src/test/java, so that the Dockerfile compiles them during the build.
    holdout_dir: str = HOLDOUT_DIR
    holdout_command: str = HOLDOUT_COMMAND


def load_suite(path: Path) -> list[Example]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    base = path.parent
    examples = []
    for raw in data.get("examples", []):
        ex = Example(
            name=raw["name"],
            repo=(base / raw["repo"]).resolve(),
            expect=raw["expect"],
            category=raw.get("category", ""),
            description=" ".join(str(raw.get("description", "")).split()),
            min_gain=float(raw.get("min_gain", 0.10)),
            tolerance=float(raw.get("tolerance", 0.05)),
            hotspots=list(raw.get("hotspots", [])),
            holdout=(base / raw["holdout"]).resolve() if raw.get("holdout") else None,
            reference=(base / raw["reference"]).resolve() if raw.get("reference") else None,
            holdout_dir=raw.get("holdout_dir", HOLDOUT_DIR),
            holdout_command=raw.get("holdout_command", HOLDOUT_COMMAND),
        )
        if Path(ex.holdout_dir).is_absolute() or ".." in Path(ex.holdout_dir).parts:
            raise SystemExit(f"{ex.name}: holdout_dir must be relative and inside the repository")
        if ex.expect not in EXPECTATIONS:
            raise SystemExit(f"{ex.name}: expect must be one of {EXPECTATIONS}, not {ex.expect!r}")
        for p in (ex.repo, ex.holdout, ex.reference):
            if p is not None and not p.exists():
                raise SystemExit(f"{ex.name}: {p} does not exist")
        examples.append(ex)
    names = [e.name for e in examples]
    if len(names) != len(set(names)):
        raise SystemExit("Example names in the suite must be unique.")
    return examples


def select(examples: list[Example], only: str | None) -> list[Example]:
    if not only:
        return examples
    wanted = [n.strip() for n in only.split(",") if n.strip()]
    unknown = set(wanted) - {e.name for e in examples}
    if unknown:
        raise SystemExit(f"Unknown examples: {', '.join(sorted(unknown))}")
    return [e for e in examples if e.name in wanted]


# -- Running in containers (or locally) ----------------------------------------------
class LocalRunner:
    """Runs commands without Docker in the working directory – for development only.

    No isolation, no limits; PYTHONPATH points to src/ of the copy.
    """

    def build(self, context: Path, dockerfile: str, tag: str) -> str:
        return str(context)

    def start(self, image: str, limits) -> _LocalBox:
        return _LocalBox(Path(image))


class _LocalBox:
    id = "local"

    def __init__(self, path: Path) -> None:
        self.path = path

    def exec(self, command: str, timeout_s: int, env: dict[str, str] | None = None) -> ExecResult:
        full_env = {**os.environ, "PYTHONPATH": str(self.path / "src"), "PYTHONDONTWRITEBYTECODE": "1"}
        full_env.update(env or {})
        start = time.perf_counter()
        try:
            proc = subprocess.run(
                ["sh", "-c", command], cwd=self.path, env=full_env, capture_output=True, text=True,
                timeout=timeout_s,
            )  # fmt: skip
            code, output = proc.returncode, proc.stdout + proc.stderr
        except subprocess.TimeoutExpired as exc:
            code, output = 137, f"Time limit exceeded: {exc}"
        return ExecResult(code, output, time.perf_counter() - start)

    def cpu_seconds(self) -> float | None:
        return None

    def remove(self) -> None:
        pass


def make_runner(local: bool):
    if local:
        return LocalRunner()
    from joulecode.container import DockerRunner

    return DockerRunner()


def prepare_copy(
    repo: Path,
    dst: Path,
    patch: str | Path | None = None,
    holdout: Path | None = None,
    holdout_dir: str = HOLDOUT_DIR,
) -> Path:
    """Copy of the example, optionally with patch and holdout tests (under `holdout_dir`)."""
    shutil.copytree(repo, dst, ignore=IGNORE)
    if patch:
        patch_file = Path(patch) if isinstance(patch, Path) else dst.parent / f"{dst.name}.patch"
        if not isinstance(patch, Path):
            patch_file.write_text(patch, encoding="utf-8")
        proc = subprocess.run(
            ["git", "apply", "--whitespace=nowarn", str(patch_file.resolve())],
            cwd=dst, capture_output=True, text=True,
        )  # fmt: skip
        if proc.returncode != 0:
            raise ValueError(f"Patch cannot be applied: {proc.stderr.strip()}")
    if holdout:
        shutil.copytree(holdout, dst / holdout_dir, ignore=IGNORE, dirs_exist_ok=True)
    return dst


def run_command(runner, path: Path, cfg, command: str, timeout_s: int, env: dict | None = None) -> ExecResult:
    image = runner.build(path / cfg.build.context, cfg.build.dockerfile, image_tag("suite", path))
    box = runner.start(image, cfg.limits)
    try:
        return box.exec(command, timeout_s, env or {})
    finally:
        box.remove()


def _tail(text: str, n: int = 25) -> str:
    return "\n".join(text.strip().splitlines()[-n:])


def _checksum(output: str) -> str | None:
    m = re.search(r"checksum=(\S+)", output)
    return m.group(1) if m else None


# -- check -------------------------------------------------------------------------
def cmd_check(args) -> int:
    examples = select(load_suite(Path(args.suite)), args.only)
    runner = make_runner(args.local)
    rows, problems = [], []
    for ex in examples:
        cfg = load_config(ex.repo)
        print(f"\n== {ex.name} ==")
        row = {"name": ex.name}
        with tempfile.TemporaryDirectory(prefix="joulecode-suite-") as tmp:
            variants = [("Original", None)] + ([("Reference", ex.reference)] if ex.reference else [])
            for label, patch in variants:
                try:
                    path = prepare_copy(ex.repo, Path(tmp) / label, patch, ex.holdout, ex.holdout_dir)
                except ValueError as exc:
                    problems.append(f"{ex.name}/{label}: {exc}")
                    row[label] = {"error": str(exc)}
                    print(f"  {label}: {exc}")
                    continue
                res = {}
                tests = run_command(runner, path, cfg, cfg.test.command, cfg.test.timeout_s, cfg.test.env)
                res["tests"] = tests.ok
                if not tests.ok:
                    problems.append(f"{ex.name}/{label}: visible tests red\n{_tail(tests.output)}")
                if ex.holdout:
                    hold = run_command(runner, path, cfg, ex.holdout_command, cfg.test.timeout_s)
                    res["holdout"] = hold.ok
                    if not hold.ok:
                        problems.append(f"{ex.name}/{label}: Holdout-Tests rot\n{_tail(hold.output)}")
                if not args.no_bench:
                    print(f"  {label}: Benchmark …", flush=True)
                    bench = run_command(
                        runner, path, cfg, cfg.benchmark.command, cfg.benchmark.timeout_s, cfg.benchmark.env
                    )
                    res["bench_s"] = round(bench.wall_s, 2)
                    res["bench_ok"] = bench.ok
                    res["checksum"] = _checksum(bench.output)
                    if not bench.ok:
                        problems.append(f"{ex.name}/{label}: benchmark failed\n{_tail(bench.output)}")
                if label == "Original" and not args.no_bench:
                    res["hotspots"] = check_hotspots(ex, cfg, runner, path, problems)
                row[label] = res
                print(f"  {label}: " + ", ".join(f"{k}={v}" for k, v in res.items()))
        orig = row.get("Original", {})
        if orig.get("bench_s") and orig.get("bench_ok"):
            minimum = cfg.benchmark.min_duration_s
            rounds = cfg.benchmark.env.get("BENCH_ROUNDS")
            if orig["bench_s"] < minimum:
                hint = ""
                if rounds and rounds.isdigit():
                    hint = f" – suggestion: BENCH_ROUNDS {math.ceil(int(rounds) * minimum * 1.25 / orig['bench_s'])}"
                problems.append(
                    f"{ex.name}: benchmark takes {orig['bench_s']:.1f} s < min_duration_s {minimum:.0f} s{hint}"
                )
            elif rounds and rounds.isdigit() and orig["bench_s"] > 3 * minimum:
                print(
                    f"  Note: {orig['bench_s']:.0f} s per run is plenty; "
                    f"BENCH_ROUNDS {math.ceil(int(rounds) * minimum * 1.25 / orig['bench_s'])} would suffice."
                )
        ref = row.get("Reference", {})
        if orig.get("bench_s") and ref.get("bench_s"):
            row["reference_change"] = ref["bench_s"] / orig["bench_s"] - 1
            row["checksum_equal"] = orig.get("checksum") == ref.get("checksum")
        rows.append(row)

    print("\n" + render_check(rows, args.local))
    if problems:
        print("Problems:\n- " + "\n- ".join(problems))
        return 1
    print("All examples OK.")
    return 0


def check_hotspots(ex: Example, cfg, runner, path: Path, problems: list[str]) -> list[str]:
    """Profiling as in `joulecode optimize`: which hotspots would joule:code work on?"""
    from joulecode.lang import get_language

    image = runner.build(path / cfg.build.context, cfg.build.dockerfile, image_tag("suite", path))
    prof = get_language(cfg.language).profile(cfg, runner, image, path, log=lambda _: None)
    if not prof.ok:
        problems.append(f"{ex.name}: profiling failed\n{_tail(prof.error or '')}")
        return []
    found = [h.qualname for h in prof.hotspots]
    shares = ", ".join(f"{h.qualname} {h.share:.0%}" for h in prof.hotspots)
    print(f"  Hotspots: {shares or 'none'}")
    if ex.expect == "no_hotspots" and found:
        problems.append(f"{ex.name}: no hotspots expected, found: {', '.join(found)}")
    if ex.hotspots and not any(_matches(f, e) for f in found for e in ex.hotspots):
        problems.append(
            f"{ex.name}: none of the expected hotspots ({', '.join(ex.hotspots)}) found, "
            f"but: {', '.join(found) or 'none'}"
        )
    return found


def render_check(rows: list[dict], local: bool) -> str:
    def mark(v):
        return "–" if v is None else ("ok" if v else "RED")

    lines = [
        "Check of the examples" + (" (local, without Docker – run times only rough)" if local else ""),
        "",
        f"{'Example':<12} {'Tests':>6} {'Holdout':>8} {'Run time':>9} │ {'Ref.tests':>9} {'Ref.holdout':>11} "
        f"{'Ref.run time':>12} {'Change':>9} {'Checksum':>9}",
    ]
    for r in rows:
        o, f = r.get("Original", {}), r.get("Reference", {})
        change = f"{r['reference_change'] * 100:+.0f}%" if "reference_change" in r else "–"
        same = "–" if "checksum_equal" not in r else ("equal" if r["checksum_equal"] else "different")
        lines.append(
            f"{r['name']:<12} {mark(o.get('tests')):>6} {mark(o.get('holdout')):>8} "
            f"{(str(o['bench_s']) + ' s') if o.get('bench_s') else '–':>9} │ "
            f"{mark(f.get('tests')) if f else '–':>9} {mark(f.get('holdout')) if f else '–':>11} "
            f"{(str(f['bench_s']) + ' s') if f.get('bench_s') else '–':>12} {change:>9} {same:>9}"
        )
    lines.append("")
    lines.append("Hotspots joule:code would work on (profiling of the original):")
    lines += [
        f"  {r['name']:<12} {', '.join(r.get('Original', {}).get('hotspots', [])) or '–'}" for r in rows
    ]
    lines.append("")
    lines.append(
        "“Checksum different” means: reference and original do not compute the same – normal in the last digits for floating point (sigproc), otherwise check it."
    )
    return "\n".join(lines)


# -- run ---------------------------------------------------------------------------
def _stream(cmd: list[str], log_path: Path) -> int:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w", encoding="utf-8") as log:
        log.write("$ " + " ".join(cmd) + "\n")
        env = {**os.environ, "PYTHONUNBUFFERED": "1"}  # show progress immediately
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1, env=env
        )
        assert proc.stdout is not None
        try:
            for line in proc.stdout:
                sys.stdout.write("  │ " + line)
                log.write(line)
            return proc.wait()
        except KeyboardInterrupt:
            proc.terminate()  # joulecode catches this and cleans up
            try:
                proc.wait(timeout=120)
            except subprocess.TimeoutExpired:
                proc.kill()
            raise


def measure_args(extra: list[str]) -> list[str]:
    """The arguments from `extra` that `joulecode measure` understands as well."""
    out, i = [], 0
    while i < len(extra):
        opt = extra[i].split("=", 1)[0]
        takes_value = MEASURE_OPTIONS.get(opt)
        if takes_value is None:
            i += 1
            continue
        out.append(extra[i])
        if takes_value and "=" not in extra[i] and i + 1 < len(extra):
            out.append(extra[i + 1])
            i += 1
        i += 1
    return out


def run_holdout(ex: Example, run_dir: Path, runner) -> dict:
    """Holdout tests for every candidate that passed the test gate."""
    data = json.loads((run_dir / "result.json").read_text(encoding="utf-8"))
    cands = list(data.get("candidates", []))
    if data.get("combination"):
        cands.append(data["combination"])
    cfg = load_config(ex.repo)
    results = {}
    with tempfile.TemporaryDirectory(prefix="joulecode-suite-") as tmp:
        for c in cands:
            if c.get("status") not in GATE_PASSED or not c.get("patch"):
                continue
            try:
                path = prepare_copy(ex.repo, Path(tmp) / c["id"], c["patch"], ex.holdout, ex.holdout_dir)
            except ValueError as exc:
                results[c["id"]] = {"ok": False, "output": str(exc)}
                continue
            res = run_command(runner, path, cfg, ex.holdout_command, cfg.test.timeout_s)
            results[c["id"]] = {"ok": res.ok, "output": "" if res.ok else _tail(res.output, 40)}
            print(f"  Holdout {c['id']}: {'passed' if res.ok else 'FAILED'}")
    (run_dir / "holdout.json").write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    return results


def cmd_run(args) -> int:
    suite_path = Path(args.suite)
    examples = select(load_suite(suite_path), args.only)
    extra = args.extra[1:] if args.extra[:1] == ["--"] else args.extra
    out = Path(args.out or f"suite-runs/{datetime.now():%Y%m%d-%H%M%S}").resolve()
    out.mkdir(parents=True, exist_ok=True)
    meta = {
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "suite": str(suite_path.resolve()),
        "joulecode": _version(),
        "optimize_args": extra,
        "repeat": args.repeat,
        "examples": [asdict(e) for e in examples],
    }
    (out / "suite.json").write_text(
        json.dumps(meta, indent=2, ensure_ascii=False, default=str), encoding="utf-8"
    )
    runner = None if args.no_holdout else make_runner(args.local_holdout)
    print(f"Suite: {len(examples)} example(s) × {args.repeat} run(s) → {out}")

    try:
        for ex in examples:
            ex_dir = out / ex.name
            if args.reference and ex.reference and not (ex_dir / "reference" / "result.json").is_file():
                print(f"\n== {ex.name}: measuring the reference patch ==")
                cmd = [sys.executable, "-m", "joulecode.cli", "measure", str(ex.repo),
                       "--candidate-patch", str(ex.reference), "--out", str(ex_dir / "reference"),
                       *measure_args(extra)]  # fmt: skip
                _stream(cmd, ex_dir / "reference" / "measure.log")
            for i in range(1, args.repeat + 1):
                run_dir = ex_dir / f"run-{i}"
                if (run_dir / "result.json").is_file() and not args.overwrite:
                    print(f"\n== {ex.name} run {i}: already present, skipped ==")
                    continue
                print(f"\n== {ex.name} run {i}/{args.repeat} ({ex.category}, expected: {ex.expect}) ==")
                cmd = [
                    sys.executable,
                    "-m",
                    "joulecode.cli",
                    "optimize",
                    str(ex.repo),
                    "--out",
                    str(run_dir),
                    *extra,
                ]
                t0 = time.monotonic()
                code = _stream(cmd, run_dir / "optimize.log")
                (run_dir / "suite-run.json").write_text(
                    json.dumps({"exit_code": code, "wall_s": round(time.monotonic() - t0, 1)}),
                    encoding="utf-8",
                )
                if not (run_dir / "result.json").is_file():
                    print(f"  joulecode optimize returned no result (exit code {code}).")
                    if code == 2:
                        print("  Pre-flight check failed (LLM or measurement not ready) – suite aborted.")
                        shutil.rmtree(run_dir)  # not a real run – do not count it as an error
                        write_summary(out)
                        return 2
                elif runner is not None and ex.holdout:
                    run_holdout(ex, run_dir, runner)
                write_summary(out)
    except KeyboardInterrupt:
        print("\nAborted – finished runs are kept.")
    summary = write_summary(out)
    print(f"\nSummary: {summary}")
    return 0


def _version() -> str:
    try:
        from joulecode.cli import version_string

        return version_string()
    except Exception:  # noqa: BLE001 – informational only
        return ""


# -- Evaluation ------------------------------------------------------------------------
def _matches(found: str, expected: str) -> bool:
    return found == expected or found.endswith("." + expected)


def evaluate_run(ex: Example, run_dir: Path) -> dict:
    """Key figures and verdict for a run of `joulecode optimize`."""
    r: dict = {"example": ex.name, "run": run_dir.name, "expect": ex.expect}
    meta_file = run_dir / "suite-run.json"
    if meta_file.is_file():
        r.update(json.loads(meta_file.read_text(encoding="utf-8")))
    result_file = run_dir / "result.json"
    if not result_file.is_file():
        r.update(status="no result", verdict="ERROR", reason="joulecode optimize returned no result.json")
        return r
    data = json.loads(result_file.read_text(encoding="utf-8"))
    holdout_file = run_dir / "holdout.json"
    holdout = json.loads(holdout_file.read_text(encoding="utf-8")) if holdout_file.is_file() else None

    r["status"] = data.get("status")
    r["error"] = (data.get("error") or "")[:300]
    r["llm"] = data.get("llm", "")
    m = data.get("measurement") or {}
    r["backend"] = (m.get("backend") or {}).get("name", "")
    r["warnings"] = list(data.get("warnings", [])) + list(m.get("warnings", []))

    found = [h["qualname"] for h in (data.get("profile") or {}).get("hotspots", [])]
    r["hotspots_found"] = found
    r["hotspots_expected_hit"] = [e for e in ex.hotspots if any(_matches(f, e) for f in found)]

    cands = data.get("candidates", [])
    statuses: dict[str, int] = {}
    for c in cands:
        statuses[c["status"]] = statuses.get(c["status"], 0) + 1
    r["candidates"] = len(cands)
    r["by_status"] = statuses
    r["repaired"] = sum(1 for c in cands if c.get("origin") == "repair")
    r["repaired_ok"] = sum(1 for c in cands if c.get("origin") == "repair" and c["status"] in GATE_PASSED)
    r["revisions"] = sum(1 for c in cands if c.get("origin") == "revision")
    gate_passed = [c for c in cands if c["status"] in GATE_PASSED]
    r["gate_passed"] = len(gate_passed)

    rows = data.get("ranking", [])
    r["ranking"] = [
        {k: row[k] for k in ("id", "rel_change", "ci_low", "ci_high", "verdict", "maintainability", "pareto")}
        for row in rows
    ]
    r["holdout"] = holdout
    leaks = {cid: h for cid, h in (holdout or {}).items() if not h["ok"]}
    r["gate_leaks"] = sorted(leaks)
    better = [row for row in rows if row["verdict"] == "better"]
    clean_better = [row for row in better if row["id"] not in leaks]
    best = min(clean_better, key=lambda row: row["rel_change"], default=None)
    r["best"] = best
    leaking_better = [row["id"] for row in better if row["id"] in leaks]

    status = r["status"]
    if status in ("baseline_tests_failed", "measurement_failed") or (
        status not in ("ok", "no_hotspots", "no_candidates")
    ):
        r["verdict"], r["reason"] = "ERROR", f"pipeline status {status}: {r['error'][:120]}"
    elif leaking_better:
        r["verdict"] = "NOT MET"
        r["reason"] = (
            f"gate gap: significantly better candidate {', '.join(leaking_better)} fails the holdout"
        )
    elif ex.expect == "improvement":
        if status != "ok":
            r["verdict"], r["reason"] = "NOT MET", f"status {status}"
        elif best is None:
            r["verdict"], r["reason"] = "NOT MET", "no significantly better candidate"
        elif -best["rel_change"] < ex.min_gain:
            r["verdict"] = "NOT MET"
            r["reason"] = f"only {-best['rel_change']:.0%} saved, expected ≥ {ex.min_gain:.0%}"
        else:
            r["verdict"], r["reason"] = "MET", f"{best['id']}: {-best['rel_change']:.0%} saved"
    elif ex.expect == "no_improvement":
        false_alarm = [row for row in better if -row["rel_change"] > ex.tolerance]
        if false_alarm:
            worst = min(false_alarm, key=lambda row: row["rel_change"])
            r["verdict"] = "NOT MET"
            r["reason"] = (
                f"false alarm? {worst['id']} supposedly {-worst['rel_change']:.0%} more efficient "
                f"(tolerance {ex.tolerance:.0%}) – look at the patch"
            )
        else:
            r["verdict"], r["reason"] = "MET", f"status {status}, no saving > {ex.tolerance:.0%}"
    else:  # no_hotspots
        if status == "no_hotspots":
            r["verdict"], r["reason"] = "MET", "profiling found no hotspots"
        else:
            r["verdict"], r["reason"] = "NOT MET", f"hotspots found: {', '.join(found)}"
    if holdout is None and ex.holdout and gate_passed:
        r["reason"] += " (holdout not checked)"
    return r


def evaluate_reference(ex_dir: Path) -> dict | None:
    f = ex_dir / "reference" / "result.json"
    if not f.is_file():
        return None
    data = json.loads(f.read_text(encoding="utf-8"))
    for metrics in (data.get("comparison") or {}).values():
        prim = metrics.get("primary")
        if prim:
            return {k: prim[k] for k in ("rel_change", "ci_low", "ci_high", "verdict")}
    return {"verdict": data.get("status", "?")}


def write_summary(out: Path) -> Path:
    meta = json.loads((out / "suite.json").read_text(encoding="utf-8"))
    examples = [
        Example(**{**e, "repo": Path(e["repo"]),
                   "holdout": Path(e["holdout"]) if e.get("holdout") else None,
                   "reference": Path(e["reference"]) if e.get("reference") else None})
        for e in meta["examples"]
    ]  # fmt: skip
    runs, refs = [], {}
    for ex in examples:
        ex_dir = out / ex.name
        refs[ex.name] = evaluate_reference(ex_dir)
        run_dirs = sorted(ex_dir.glob("run-*"), key=lambda p: int(p.name.split("-")[1]))
        runs.extend(evaluate_run(ex, run_dir) for run_dir in run_dirs)
    (out / "summary.json").write_text(
        json.dumps(
            {"meta": meta, "references": refs, "runs": runs}, indent=2, ensure_ascii=False, default=str
        ),
        encoding="utf-8",
    )
    path = out / "summary.md"
    path.write_text(render_summary(meta, examples, runs, refs), encoding="utf-8")
    return path


def _pct(x: float | None) -> str:
    return "–" if x is None else f"{x * 100:+.1f}%"


def render_summary(meta: dict, examples: list[Example], runs: list[dict], refs: dict) -> str:
    lines = ["# joule:code test suite", ""]
    lines.append(f"- Started: {meta['started_at']}")
    if meta.get("joulecode"):
        lines.append(f"- {meta['joulecode']}")
    llms = sorted({r["llm"] for r in runs if r.get("llm")})
    backends = sorted({r["backend"] for r in runs if r.get("backend")})
    if llms:
        lines.append(f"- LLM: {', '.join(llms)}")
    if backends:
        lines.append(f"- Measurement back-end: {', '.join(backends)}")
    if meta.get("optimize_args"):
        lines.append(f"- Arguments for optimize: `{' '.join(meta['optimize_args'])}`")
    done = [r for r in runs if r.get("verdict")]
    ok = sum(r["verdict"] == "MET" for r in done)
    lines += ["", f"**{ok} of {len(done)} runs meet the expectation.**", ""]

    lines += [
        "## Per example",
        "",
        "| Example | Category | Expectation | met | best saving | Reference | Hotspots hit | Gate gaps |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for ex in examples:
        ex_runs = [r for r in runs if r["example"] == ex.name]
        if not ex_runs:
            continue
        bests = [r["best"]["rel_change"] for r in ex_runs if r.get("best")]
        ref = refs.get(ex.name) or {}
        hits = [len(r.get("hotspots_expected_hit", [])) for r in ex_runs if "hotspots_found" in r]
        hit_txt = f"{sum(hits)}/{len(ex.hotspots) * len(hits)}" if ex.hotspots and hits else "–"
        leaks = sum(len(r.get("gate_leaks", [])) for r in ex_runs)
        expect = ex.expect + (f" ≥ {ex.min_gain:.0%}" if ex.expect == "improvement" else "")
        lines.append(
            f"| {ex.name} | {ex.category} | {expect} | {sum(r['verdict'] == 'MET' for r in ex_runs)}/{len(ex_runs)} "
            f"| {_pct(min(bests)) if bests else '–'} | {_pct(ref.get('rel_change')) if ref else '–'} "
            f"| {hit_txt} | {leaks} |"
        )
    lines.append("")

    lines += [
        "## Runs",
        "",
        "| Example | Run | Status | Hotspots | Candidates | Tests green | measured | best | Holdout | Verdict | Reason |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in runs:
        bs = r.get("by_status", {})
        hold = r.get("holdout")
        hold_txt = "–" if hold is None else f"{sum(h['ok'] for h in hold.values())}/{len(hold)}"
        best = r.get("best")
        lines.append(
            f"| {r['example']} | [{r['run']}]({r['example']}/{r['run']}/report.html) | {r.get('status', '')} | {', '.join(r.get('hotspots_found', [])) or '–'} "
            f"| {r.get('candidates', 0)} | {r.get('gate_passed', 0)} | {bs.get('measured', 0)} "
            f"| {(best['id'] + ' ' + _pct(best['rel_change'])) if best else '–'} | {hold_txt} "
            f"| {r['verdict']} | {r.get('reason', '')} |"
        )
    lines.append("")

    total = {
        k: sum(r.get("by_status", {}).get(k, 0) for r in runs)
        for k in ("invalid", "tests_failed", "duplicate", "vetoed", "ready", "measured", "measurement_failed")
    }
    generated = sum(r.get("candidates", 0) for r in runs)
    better = sum(
        1
        for r in runs
        for row in r.get("ranking", [])
        if row["verdict"] == "better" and row["id"] != "combined"
    )
    worse = sum(1 for r in runs for row in r.get("ranking", []) if row["verdict"] == "worse")
    lines += [
        "## Funnel over all runs",
        "",
        "| Step | Count |",
        "|---|---|",
        f"| Candidates generated | {generated} |",
        f"| not applicable (syntax, name, …) | {total['invalid']} |",
        f"| Tests red (also after repair) | {total['tests_failed']} |",
        f"| Repairs, green afterwards | {sum(r.get('repaired_ok', 0) for r in runs)} of "
        f"{sum(r.get('repaired', 0) for r in runs)} repairs |",
        f"| Duplicate of another candidate | {total['duplicate']} |",
        f"| Reviewer veto | {total['vetoed']} (revisions: {sum(r.get('revisions', 0) for r in runs)}) |",
        f"| Measured | {total['measured']} |",
        f"| Significantly better | {better} |",
        f"| Significantly worse | {worse} |",
        f"| Gate gaps (tests green, holdout red) | {sum(len(r.get('gate_leaks', [])) for r in runs)} |",
        "",
    ]

    leak_runs = [r for r in runs if r.get("gate_leaks")]
    if leak_runs:
        lines += [
            "## Gate gaps",
            "",
            "These candidates passed the visible tests but not the holdout tests.",
            "",
        ]
        for r in leak_runs:
            for cid in r["gate_leaks"]:
                lines += [
                    f"### {r['example']} / {r['run']} / {cid}",
                    "",
                    f"Patch: `{r['example']}/{r['run']}/candidates/{cid}.patch`",
                    "",
                    "```",
                    r["holdout"][cid]["output"],
                    "```",
                    "",
                ]
    warned = [r for r in runs if r.get("warnings")]
    if warned:
        lines += ["## Measurement warnings", ""]
        for r in warned:
            for w in dict.fromkeys(r["warnings"]):
                lines.append(f"- {r['example']} / {r['run']}: {w}")
        lines.append("")
    return "\n".join(lines)


def cmd_evaluate(args) -> int:
    out = Path(args.dir)
    if not (out / "suite.json").is_file():
        print(f"{out} is not a suite directory (no suite.json).", file=sys.stderr)
        return 2
    if args.holdout:
        meta = json.loads((out / "suite.json").read_text(encoding="utf-8"))
        runner = make_runner(args.local_holdout)
        for e in meta["examples"]:
            if not e.get("holdout"):
                continue
            ex = Example(**{**e, "repo": Path(e["repo"]), "holdout": Path(e["holdout"]), "reference": None})
            for run_dir in sorted((out / ex.name).glob("run-*")):
                if (run_dir / "result.json").is_file():
                    print(f"{ex.name}/{run_dir.name}:")
                    run_holdout(ex, run_dir, runner)
    print(f"Summary: {write_summary(out)}")
    return 0


# -- Command line ---------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Test suite for joule:code", formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "--suite", default=str(DEFAULT_SUITE), help="suite file (default examples/suite/suite.yml)"
    )
    sub = p.add_subparsers(dest="command", required=True)

    c = sub.add_parser(
        "check", help="check the examples (tests, holdout, reference) and calibrate the benchmarks"
    )
    c.add_argument("--only", help="only these examples (comma-separated)")
    c.add_argument("--no-bench", action="store_true", help="do not run the benchmarks")
    c.add_argument(
        "--local", action="store_true", help="without Docker in the local Python (development only)"
    )
    c.set_defaults(func=cmd_check)

    r = sub.add_parser("run", help="send all examples through joulecode optimize and evaluate them")
    r.add_argument("--only", help="only these examples (comma-separated)")
    r.add_argument("--repeat", type=int, default=1, help="runs per example (the LLM is not deterministic)")
    r.add_argument("--reference", action="store_true", help="additionally measure the reference patches")
    r.add_argument(
        "--out", help="suite directory (default suite-runs/<timestamp>); existing runs are skipped"
    )
    r.add_argument("--overwrite", action="store_true", help="run existing runs in the suite directory again")
    r.add_argument("--no-holdout", action="store_true", help="do not run the holdout tests")
    r.add_argument(
        "--local-holdout", action="store_true", help="holdout tests without Docker (development only)"
    )
    r.add_argument("extra", nargs=argparse.REMAINDER, help="after -- : arguments for joulecode optimize")
    r.set_defaults(func=cmd_run)

    e = sub.add_parser("evaluate", help="regenerate the summary of a suite directory")
    e.add_argument("dir")
    e.add_argument("--holdout", action="store_true", help="run the holdout tests again")
    e.add_argument(
        "--local-holdout", action="store_true", help="holdout tests without Docker (development only)"
    )
    e.set_defaults(func=cmd_evaluate)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
