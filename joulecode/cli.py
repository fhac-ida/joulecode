"""Command line.

joulecode doctor                       # check the measurement machine
joulecode measure REPO --aa            # A/A test: how noisy is the measurement?
joulecode measure REPO --candidate-patch opt.patch
joulecode measure REPO --candidate-dir ../repo-optimised
joulecode serve                        # platform: API + web interface + job queue
joulecode submit --mode gate --base origin/main   # send from CI to the platform
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

from joulecode.config import load_config
from joulecode.energy import BACKEND_NAMES, all_backends, make_backend
from joulecode.energy.metrion_live import default_results_pattern
from joulecode.hostcheck import run_checks
from joulecode.variants import Variant, prepare_variants

SYMBOL = {"ok": "✔", "warn": "!", "fail": "✘"}


def _parse_env(items: list[str]) -> dict[str, str]:
    env = {}
    for item in items:
        key, sep, value = item.partition("=")
        if not sep:
            raise SystemExit(f"--env expects KEY=VALUE, not {item!r}")
        env[key] = value
    return env


def version_string() -> str:
    """Version plus Git state of the repository joule:code runs from (helps when comparing two machines)."""
    import subprocess

    from joulecode import __version__

    repo = Path(__file__).resolve().parent.parent
    try:
        commit = subprocess.run(
            ["git", "-C", str(repo), "log", "-1", "--format=%h %cd", "--date=format:%Y-%m-%d %H:%M"],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        commit = ""
    return f"joule:code {__version__}" + (f" (commit {commit})" if commit else "")


def cmd_doctor(args: argparse.Namespace) -> int:
    print(version_string() + "\n")
    backends = all_backends(metrion_results=args.metrion_results, rapl_idle_s=0)
    problems = {b.name: b.check() for b in backends}
    checks = run_checks(problems)
    for c in checks:
        print(f" {SYMBOL[c.status]} {c.name:<16} {c.detail}")
    best = next((b.name for b in backends if not problems[b.name]), None)
    print(f"\nMost accurate available back-end: {best}")
    return 1 if any(c.status == "fail" for c in checks) else 0


def cmd_measure(args: argparse.Namespace) -> int:
    from joulecode.container import DockerRunner
    from joulecode.protocol import measure
    from joulecode.report import write_reports

    repo = Path(args.repo).resolve()
    cfg = load_config(repo)
    _apply_measurement_args(cfg, args)

    backend = make_backend(args.backend, metrion_results=args.metrion_results, rapl_idle_s=args.rapl_idle)
    problems = backend.check()
    if problems:
        print("Back-end not ready:\n  - " + "\n  - ".join(problems), file=sys.stderr)
        if not args.force:
            return 2
    print(f"Backend: {backend.name} – {backend.quantity}")

    out = Path(args.out or f"joulecode-runs/{datetime.now():%Y%m%d-%H%M%S}")
    _archive_metrion_db(backend, out)
    with tempfile.TemporaryDirectory(prefix="joulecode-") as tmp:
        variants = prepare_variants(
            repo,
            Path(tmp),
            protected_paths=cfg.protected_paths,
            baseline_ref=args.baseline_ref,
            candidate_dir=Path(args.candidate_dir) if args.candidate_dir else None,
            candidate_patch=Path(args.candidate_patch) if args.candidate_patch else None,
        )
        if args.aa:
            if len(variants) > 1:
                raise SystemExit("--aa only makes sense without a candidate.")
            variants.append(Variant("baseline-b", variants[0].path, ["A/A control: identical to baseline"]))
        result = measure(
            cfg, variants, DockerRunner(), backend, bench_env=_parse_env(args.env),
            checkpoint=lambda r: write_reports(r, out),
        )  # fmt: skip

    json_path, md_path = write_reports(result, out)
    print(f"\nStatus: {result.status}")
    for cand, metrics in result.comparison.items():
        prim = metrics.get("primary")
        if not prim:
            continue
        print(f"{cand}: ", end="")
        print(
            f"primary metric: {prim['rel_change'] * 100:+.1f}% "
            f"(95% CI {prim['ci_low'] * 100:+.1f} … {prim['ci_high'] * 100:+.1f}%), {prim['verdict']}"
        )
    for w in result.warnings:
        print(f"Warning: {w}")
    print(f"Report: {md_path}\nHTML:   {md_path.with_suffix('.html')}\nData:   {json_path}")
    return 0 if result.status == "ok" else 1


def _archive_metrion_db(backend, out: Path) -> None:
    """Put metrion's raw data into the result directory as well (for later re-analysis)."""
    if hasattr(backend, "archive_dir"):
        backend.archive_dir = out / "metrion"


def _apply_measurement_args(cfg, args) -> None:
    if args.repetitions:
        cfg.measurement.repetitions = args.repetitions
    if args.min_repetitions:
        cfg.measurement.min_repetitions = args.min_repetitions
    if args.fixed:
        cfg.measurement.adaptive = False
    if args.warmup is not None:
        cfg.measurement.warmup = args.warmup
    if args.cooldown is not None:
        cfg.measurement.cooldown_s = args.cooldown
    if args.seed is not None:
        cfg.measurement.seed = args.seed


def cmd_analyze(args: argparse.Namespace) -> int:
    import json

    from joulecode.analysis import render_analysis_markdown
    from joulecode.container import DockerRunner, image_tag
    from joulecode.lang import get_language

    repo = Path(args.repo).resolve()
    cfg = load_config(repo)
    if args.hotspots:
        cfg.optimize.hotspots = args.hotspots
    lang = get_language(cfg.language)
    static = lang.analyze_repo(repo, cfg.protected_paths)
    prof = None
    if not args.no_profile:
        runner = DockerRunner()
        with tempfile.TemporaryDirectory(prefix="joulecode-") as tmp:
            (baseline,) = prepare_variants(repo, Path(tmp), protected_paths=cfg.protected_paths)
            image = runner.build(
                baseline.path / cfg.build.context, cfg.build.dockerfile, image_tag("baseline", baseline.path)
            )
            prof = lang.profile(cfg, runner, image, baseline.path).to_dict()
    out = Path(args.out or f"joulecode-runs/analysis-{datetime.now():%Y%m%d-%H%M%S}")
    out.mkdir(parents=True, exist_ok=True)
    (out / "analysis.json").write_text(
        json.dumps({"static": static.to_dict(), "profile": prof}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    md = out / "analysis.md"
    md.write_text(render_analysis_markdown(static, prof), encoding="utf-8")
    if prof and prof.get("hotspots"):
        print("Hotspots:")
        for h in prof["hotspots"]:
            print(f"  {h['share']:>5.0%}  {h['qualname']}  ({h['file']}:{h['line']})")
    print(f"Report: {md}")
    return 0


def cmd_optimize(args: argparse.Namespace) -> int:
    from joulecode.container import DockerRunner
    from joulecode.llm import OllamaLLM
    from joulecode.optimize import Pipeline
    from joulecode.optimize_report import write_optimize_reports

    repo = Path(args.repo).resolve()
    cfg = load_config(repo)
    _apply_measurement_args(cfg, args)
    if args.hotspots:
        cfg.optimize.hotspots = args.hotspots
    if args.candidates:
        cfg.optimize.candidates_per_hotspot = args.candidates

    llm = OllamaLLM(host=args.ollama_host, model=args.model)
    problems = llm.check()
    backend = make_backend(args.backend, metrion_results=args.metrion_results, rapl_idle_s=args.rapl_idle)
    if not args.no_measure:
        problems += backend.check()
    if problems:
        print("Not ready:\n  - " + "\n  - ".join(problems), file=sys.stderr)
        if not args.force:
            return 2
    print(f"LLM: {llm.name}  |  Backend: {backend.name} – {backend.quantity}")

    out = Path(args.out or f"joulecode-runs/optimize-{datetime.now():%Y%m%d-%H%M%S}")
    _archive_metrion_db(backend, out)
    with tempfile.TemporaryDirectory(prefix="joulecode-") as tmp:
        pipeline = Pipeline(
            cfg, DockerRunner(), backend, llm, on_checkpoint=lambda r: write_optimize_reports(r, out)
        )
        result = pipeline.run(
            repo, Path(tmp), baseline_ref=args.baseline_ref, bench_env=_parse_env(args.env),
            measure_candidates=not args.no_measure,
        )  # fmt: skip
        report = write_optimize_reports(result, out)

    print(f"\nStatus: {result.status}")
    for r in result.ranking:
        star = "★" if r["pareto"] else " "
        print(
            f" {star} {r['id']}: {r['rel_change'] * 100:+.1f}% ({r['verdict']}), maintainability {r['maintainability']} – {r['title']}"
        )
    print(f"Report: {report}\nHTML:   {report.with_suffix('.html')}")
    return 0 if result.status == "ok" else 1


def _pick_archive(archives: list[Path], measurement) -> Path | None:
    """The archived metrion database that was created right after the last run of the measurement."""
    from datetime import datetime as dt

    ends = [dt.fromisoformat(r["t_end"]).timestamp() for r in measurement.runs if r.get("t_end")]
    if not ends:
        return None
    after = [a for a in archives if a.stat().st_mtime >= max(ends) - 1]
    return min(after, key=lambda a: a.stat().st_mtime) if after else None


def _pick_metrion_run(paths: list[Path], measurement, prefer_raw: bool = False) -> Path | None:
    """The metrion run (≥ 0.2) that started last before the first measurement run.

    Default: live result before raw data (small, fast). `prefer_raw`: raw data (`monitor_*`)
    first – their subsequent analysis knows all threads, including those that the
    live attribution did not know yet when a window was written.
    """
    from datetime import datetime as dt

    from joulecode.energy.metrion_cli import run_stamp

    starts = [dt.fromisoformat(r["t_start"]) for r in measurement.runs if r.get("t_start")]
    if not starts:
        return None
    first = min(starts)
    found = [
        (stamp, p.name.startswith("monitor_" if prefer_raw else "energy_"), p)
        for p in paths
        if (stamp := run_stamp(p)) and stamp <= first
    ]
    return max(found, key=lambda x: (x[0], x[1]))[2] if found else None


def cmd_reanalyze(args: argparse.Namespace) -> int:
    """Repeat the analysis of a measurement without measuring again (metrion database from the archive)."""
    import json

    from joulecode.config import ProjectConfig
    from joulecode.energy.metrion_cli import MetrionCliBackend
    from joulecode.energy.metrion_db import MetrionDbBackend
    from joulecode.protocol import load_result, reevaluate, run_handles
    from joulecode.report import write_reports

    out = Path(args.dir)
    if not (out / "result.json").is_file():
        recent = sorted(Path("joulecode-runs").glob("*/result.json"), key=lambda p: p.stat().st_mtime)[-5:]
        hint = "\n".join(f"  joulecode reanalyze {p.parent}" for p in reversed(recent))
        print(
            f"{out} is not a result directory (no result.json). This means the directory under joulecode-runs/ "
            "that measure/optimize names at the end" + (f", e.g.:\n{hint}" if hint else "."),
            file=sys.stderr,
        )
        return 2
    data = json.loads((out / "result.json").read_text(encoding="utf-8"))

    cli = MetrionCliBackend()
    if Path(cli.binary).is_file() and cli.supports_cli():
        # metrion ≥ 0.2: archived live results, otherwise the runs at the service's location
        candidates = sorted((out / "metrion").glob("energy_*"))
        candidates += sorted(cli.workdir.glob("energy_*")) + sorted(cli.workdir.glob("monitor_*"))

        def pick(measurement):
            return _pick_metrion_run(candidates, measurement, prefer_raw=args.raw)

        def read(path, measurement):
            return cli.read_output(path, run_handles(measurement))
    else:
        legacy = MetrionDbBackend()
        problems = [p for p in legacy.check() if "already running" not in p and "sudo" not in p]
        if problems:
            print("metrion analysis not possible:\n  - " + "\n  - ".join(problems), file=sys.stderr)
            return 2
        archives = sorted((out / "metrion").glob("metrion-*.db"))
        if legacy.db_path.is_file():
            # If the analysis was aborted before archiving, the database is still
            # at the service's location (until the next measurement).
            archives.append(legacy.db_path)

        def pick(measurement):
            return _pick_archive(archives, measurement)

        def read(path, measurement):
            return legacy.read_db(path, run_handles(measurement))

    def redo(measurement) -> bool:
        import dataclasses

        # With a top-up measurement, a measurement has two back-end sessions and thus two metrion results.
        sessions = sorted({r.get("session", 1) for r in measurement.runs})
        if args.db and len(sessions) > 1:
            print(
                "This measurement has a top-up measurement (2 metrion runs); --db only works without one.",
                file=sys.stderr,
            )
            return False
        readings = {}
        for no in sessions:
            part = dataclasses.replace(
                measurement, runs=[r for r in measurement.runs if r.get("session", 1) == no]
            )
            source = Path(args.db) if args.db else pick(part)
            if source is None:
                print("No matching metrion result found (specify one with --db).", file=sys.stderr)
                return False
            print(f"Analysing {source} …")
            readings.update(read(source, part))
        cfg = ProjectConfig.model_validate(measurement.config)
        reevaluate(measurement, readings, cfg)
        return True

    if "candidates" in data:  # result of joulecode optimize
        from joulecode.optimize import rank
        from joulecode.optimize_report import write_optimize_reports

        result = load_optimize_result(data)
        cands = result.candidates
        if result.measurement is None or not result.measurement.runs:
            print("This result contains no measurement runs.", file=sys.stderr)
            return 1
        if not redo(result.measurement):
            return 1
        cm = result.combination_measurement
        if cm is not None and cm.runs and redo(cm):
            result.combination.status = "measured"
            result.ranking += rank([result.combination], cm)
        measured = [c for c in cands if c.id in {v["name"] for v in result.measurement.variants}]
        for c in measured:
            c.status = "measured"
        result.ranking = rank(measured, result.measurement) + [
            r for r in result.ranking if r["id"] == "combined"
        ]
        result.ranking = list({r["id"]: r for r in result.ranking}.values())
        result.status, result.error = "ok", None
        if result.combination is None:
            result.warnings.append(
                "The combination of the best candidates was not measured (the measurement aborted before)."
            )
        report = write_optimize_reports(result, out)
        for r in result.ranking:
            star = "★" if r["pareto"] else " "
            print(f" {star} {r['id']}: {r['rel_change'] * 100:+.1f}% ({r['verdict']}) – {r['title']}")
    else:  # result of joulecode measure
        result = load_result(data)
        if not redo(result):
            return 1
        _, report = write_reports(result, out)
        for cand, metrics in result.comparison.items():
            prim = metrics.get("primary")
            if prim:
                print(f"{cand}: {prim['rel_change'] * 100:+.1f}% ({prim['verdict']})")
    print(f"Report: {report}\nHTML:   {report.with_suffix('.html')}")
    return 0


def load_optimize_result(data: dict):
    """OptimizeResult from the result.json of `joulecode optimize`."""
    from joulecode.agents import Candidate
    from joulecode.optimize import OptimizeResult
    from joulecode.protocol import load_result

    cands = [
        Candidate(**{**c, "path": Path(c["path"]) if c.get("path") else None}) for c in data["candidates"]
    ]
    combo = data.get("combination")
    return OptimizeResult(
        **{
            **data,
            "candidates": cands,
            "measurement": load_result(data["measurement"]) if data.get("measurement") else None,
            "combination": Candidate(**{**combo, "path": None}) if combo else None,
            "combination_measurement": load_result(data["combination_measurement"])
            if data.get("combination_measurement")
            else None,
        }
    )


def cmd_report(args: argparse.Namespace) -> int:
    """Regenerate reports (Markdown + HTML) from an existing result.json – without a new analysis."""
    import json

    from joulecode.optimize_report import write_optimize_reports
    from joulecode.protocol import load_result
    from joulecode.report import write_reports

    out = Path(args.dir)
    if not (out / "result.json").is_file():
        print(f"{out} is not a result directory (no result.json).", file=sys.stderr)
        return 2
    data = json.loads((out / "result.json").read_text(encoding="utf-8"))
    if "candidates" in data:
        report = write_optimize_reports(load_optimize_result(data), out)
    else:
        _, report = write_reports(load_result(data), out)
    print(f"Report: {report}\nHTML:   {report.with_suffix('.html')}")
    return 0


def cmd_cleanup(args: argparse.Namespace) -> int:
    from joulecode.cleanup import active_run, cleanup, render
    from joulecode.energy.metrion_db import MetrionDbBackend

    running = active_run()
    if running and not args.force:
        print(f"Aborting: {running} is still running. Stop it first (or use --force).", file=sys.stderr)
        return 2

    notes = []

    def stop_metrion() -> bool:
        backend = MetrionDbBackend()
        try:
            if backend._state() in ("deactivating", "activating"):
                backend._wait_until_stopped()  # already shutting down
                return True
            if backend._state() != "active":
                return False
            backend._stop_with_progress()
            return True
        except (OSError, RuntimeError) as exc:
            notes.append(f"metrion service not stopped: {exc}")
            return False

    report = cleanup(
        images=args.images,
        runs_older_than_days=args.runs_older_than,
        stop_metrion=None if args.keep_metrion else stop_metrion,
        dry_run=args.dry_run,
    )
    report.notes += notes
    if args.dry_run:
        print("Dry run – nothing was deleted.\n")
    print(render(report, args.dry_run))
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    try:
        import uvicorn

        from joulecode.server.app import ServerConfig, create_app, default_data_dir, load_admin_token
        from joulecode.server.measuring import GateProcessor, MeasureSettings, OptimizeProcessor
        from joulecode.server.processing import DummyProcessor
        from joulecode.server.review import ModeRouter, ReviewProcessor
    except ImportError as exc:
        print(f"The server needs {exc.name}: pip install 'joulecode[server]'", file=sys.stderr)
        return 2

    data_dir = Path(args.data_dir).expanduser() if args.data_dir else default_data_dir()
    admin_token, created = load_admin_token(data_dir)
    print(version_string())
    print(f"Data:         {data_dir}")
    dummy = DummyProcessor(delay_s=args.dummy_delay)
    settings = MeasureSettings(
        backend=args.backend,
        metrion_results=args.metrion_results,
        rapl_idle_s=args.rapl_idle,
        ollama_host=args.ollama_host,
        model=args.model,
        lock_wait_s=args.lock_wait,
    )
    if args.processor == "dummy":
        processor = ModeRouter({"review": dummy, "gate": dummy, "optimize": dummy})
    elif args.processor == "review":
        processor = ModeRouter({"review": ReviewProcessor(), "gate": dummy, "optimize": dummy})
    else:
        processor = ModeRouter(
            {
                "review": ReviewProcessor(),
                "gate": GateProcessor(settings),
                "optimize": OptimizeProcessor(settings),
            }
        )
    modes = ", ".join(f"{m}: {p}" for m, p in processor.describe().items())
    print(f"Processing:   {modes}")
    if args.processor == "full":
        _serve_readiness(settings)
    if created:
        print(f"Admin token:  {admin_token}\n              (saved in {data_dir / 'admin-token'})")
    else:
        print(f"Admin token:  in {data_dir / 'admin-token'} or $JOULECODE_ADMIN_TOKEN")
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print(
            f"Caution: the server listens on {args.host}. Reading is possible without a token, "
            "and gate/optimize execute submitted code – use only in trusted networks.",
            file=sys.stderr,
        )
    print(
        f"Web interface: http://{'localhost' if args.host in ('127.0.0.1', '::1') else args.host}:{args.port}/\n"
    )
    config = ServerConfig(
        data_dir=data_dir,
        admin_token=admin_token,
        max_upload_mb=args.max_upload_mb,
        processor=processor,
    )
    uvicorn.run(create_app(config), host=args.host, port=args.port, log_level=args.log_level)
    return 0


def _serve_readiness(settings) -> None:
    """Say at start-up whether gate/optimize could run right away – otherwise jobs only fail later."""
    try:
        backend = settings.make_backend()
        problems = backend.check()
        print(f"Back-end:     {backend.name}" + (" – NOT ready: " + "; ".join(problems) if problems else ""))
    except Exception as exc:  # noqa: BLE001 - informational only
        print(f"Back-end:     cannot be checked ({exc})")
    try:
        llm = settings.make_llm()
        problems = llm.check()
        print(f"LLM:          {llm.name}" + (" – NOT ready: " + "; ".join(problems) if problems else ""))
    except Exception as exc:  # noqa: BLE001 - informational only
        print(f"LLM:          cannot be checked ({exc})")


def cmd_submit(args: argparse.Namespace) -> int:
    from joulecode.submit import cmd_submit as run

    return run(args)


def _default_backend() -> str:
    """Default of --backend: $JOULECODE_BACKEND (e.g. from .joulecode.env) or auto."""
    name = os.environ.get("JOULECODE_BACKEND", "").strip() or "auto"
    if name not in BACKEND_NAMES:
        raise SystemExit(f"JOULECODE_BACKEND={name!r} is not one of: {', '.join(BACKEND_NAMES)}")
    return name


def _add_measurement_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--baseline-ref", help="Git ref of the original (default: working directory)")
    p.add_argument(
        "--backend",
        default=_default_backend(),
        choices=BACKEND_NAMES,
        help="default $JOULECODE_BACKEND or auto",
    )
    p.add_argument("--metrion-results", help="glob of the metrion-live result directories")
    p.add_argument("--rapl-idle", type=float, default=10.0, help="seconds of idle measurement for rapl")
    p.add_argument("--repetitions", type=int, help="maximum number of repetitions (adaptive) or fixed number")
    p.add_argument("--min-repetitions", type=int, help="earliest stop for adaptive measurement")
    p.add_argument("--fixed", action="store_true", help="always measure all repetitions (no early stop)")
    p.add_argument("--warmup", type=int)
    p.add_argument("--cooldown", type=float)
    p.add_argument("--seed", type=int)
    p.add_argument("--env", action="append", default=[], help="additional benchmark variable KEY=VALUE")
    p.add_argument("--out", help="output directory for reports and data")
    p.add_argument("--force", action="store_true", help="start despite problems in the pre-flight check")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="joulecode", description="Measure the energy efficiency of software.")
    p.add_argument("--version", action="version", version=version_string())
    sub = p.add_subparsers(dest="command", required=True)

    d = sub.add_parser("doctor", help="check the measurement machine")
    d.add_argument(
        "--metrion-results",
        help=f"glob of the metrion result directories (default {default_results_pattern()})",
    )
    d.set_defaults(func=cmd_doctor)

    m = sub.add_parser("measure", help="build, test and measure the original (and candidate)")
    m.add_argument("repo", help="repository with joulecode.yml")
    g = m.add_mutually_exclusive_group()
    g.add_argument("--candidate-dir", help="directory with the optimised variant")
    g.add_argument("--candidate-patch", help="patch (git diff) against the original")
    g.add_argument("--aa", action="store_true", help="measure the original against itself (noise control)")
    _add_measurement_args(m)
    m.set_defaults(func=cmd_measure)

    a = sub.add_parser("analyze", help="static analysis + hotspots via profiling")
    a.add_argument("repo")
    a.add_argument("--no-profile", action="store_true", help="static analysis only, without Docker")
    a.add_argument("--hotspots", type=int, help="number of hotspots (overrides optimize.hotspots)")
    a.add_argument("--out")
    a.set_defaults(func=cmd_analyze)

    o = sub.add_parser("optimize", help="optimise hotspots, check, measure and compare")
    o.add_argument("repo")
    o.add_argument("--ollama-host", help="default $JOULECODE_OLLAMA_HOST or http://localhost:11434")
    o.add_argument("--model", help="default $JOULECODE_OLLAMA_MODEL or qwen2.5-coder:14b")
    o.add_argument("--hotspots", type=int)
    o.add_argument("--candidates", type=int, help="candidates per hotspot")
    o.add_argument("--no-measure", action="store_true", help="only generate and check candidates")
    _add_measurement_args(o)
    o.set_defaults(func=cmd_optimize)

    r = sub.add_parser("reanalyze", help="repeat the analysis without measuring again (metrion)")
    r.add_argument("dir", help="result directory of measure or optimize")
    r.add_argument("--db", help="metrion result (live directory, run or .db); default: automatic")
    r.add_argument(
        "--raw",
        action="store_true",
        help="analyse raw data (monitor_*) instead of the live result – slower, but without gaps in the live attribution",
    )
    r.set_defaults(func=cmd_reanalyze)

    rp = sub.add_parser("report", help="regenerate reports (Markdown + HTML) from result.json")
    rp.add_argument("dir", help="result directory of measure or optimize")
    rp.set_defaults(func=cmd_report)

    c = sub.add_parser("cleanup", help="remove measurement containers, temp directories etc. of aborted runs")
    c.add_argument("--images", action="store_true", help="also delete the built variant images")
    c.add_argument(
        "--runs-older-than", type=float, metavar="DAYS", help="delete result directories older than DAYS"
    )
    c.add_argument(
        "--keep-metrion", action="store_true", help="do not stop a metrion service that is still running"
    )
    c.add_argument("--dry-run", action="store_true", help="only show what would be removed")
    c.add_argument("--force", action="store_true", help="even if a run seems active")
    c.set_defaults(func=cmd_cleanup)

    sv = sub.add_parser("serve", help="start the platform: API, web interface and job queue")
    sv.add_argument("--host", default="127.0.0.1", help="default 127.0.0.1 (reachable locally only)")
    sv.add_argument("--port", type=int, default=8765)
    sv.add_argument(
        "--data-dir", help="database and uploads (default $JOULECODE_DATA or ~/.joulecode/server)"
    )
    sv.add_argument("--max-upload-mb", type=int, default=200)
    sv.add_argument(
        "--processor",
        choices=["full", "review", "dummy"],
        default="full",
        help="full: all modes real (measurement machine); review: only review real, gate/optimize dummy "
        "(machine without measurement); dummy: everything dummy",
    )
    sv.add_argument(
        "--backend",
        default=_default_backend(),
        choices=BACKEND_NAMES,
        help="measurement back-end for gate/optimize (default $JOULECODE_BACKEND or auto)",
    )
    sv.add_argument("--metrion-results", help="glob of the metrion-live result directories")
    sv.add_argument("--rapl-idle", type=float, default=10.0, help="seconds of idle measurement for rapl")
    sv.add_argument(
        "--ollama-host", help="optimize: default $JOULECODE_OLLAMA_HOST or http://localhost:11434"
    )
    sv.add_argument("--model", help="optimize: default $JOULECODE_OLLAMA_MODEL or qwen2.5-coder:14b")
    sv.add_argument(
        "--lock-wait",
        type=float,
        default=3600,
        metavar="SECONDS",
        help="how long a job waits while a joulecode measure/optimize started by hand is running",
    )
    sv.add_argument("--dummy-delay", type=float, default=1.5, help="seconds per phase in dummy mode")
    sv.add_argument("--log-level", default="info", choices=["critical", "error", "warning", "info", "debug"])
    sv.set_defaults(func=cmd_serve)

    from joulecode.submit import add_arguments as add_submit_arguments

    sm = sub.add_parser("submit", help="send a repository to a joule:code server (CI)")
    add_submit_arguments(sm)
    sm.set_defaults(func=cmd_submit)
    return p


LOCKING_COMMANDS = {"measure", "analyze", "optimize"}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command not in LOCKING_COMMANDS:
        return args.func(args)

    from joulecode.cleanup import acquire_lock, active_run, release_lock

    running = active_run()
    if running:
        # Two runs at the same time would distort each other's measurement
        # (and fight over the metrion service).
        print(f"Aborting: {running} is already running.", file=sys.stderr)
        return 2
    acquire_lock(args.command)
    try:
        return args.func(args)
    finally:
        release_lock()


if __name__ == "__main__":
    raise SystemExit(main())
