#!/usr/bin/env python3
"""Investigate measurement dropouts of metrion: where did the energy of a run go?

Runs with the Python from metrion's own environment (polars is installed there):

    sudo /opt/metrion/.venv/bin/python scripts/metrion_diagnose.py joulecode-runs/optimize-…

Reads `result.json` of the result directory (measure or optimize) and the archived
live result under `<directory>/metrion/energy_*` (or `--live PATH`). For every run
the dynamic energy in the run's time windows is split into:

* attributed   – rows whose cgroup_name contains the container ID (this is what joule:code counts)
* no identity  – rows without cgroup_name/process (metrion did not know the thread yet when
                 writing the window)
* unknown      – rows with cgroup_name "unknown" (the cgroup ID could not be resolved)
* benchmark elsewhere – rows whose command line matches the benchmark but that lie in
                 another cgroup

The windows are 10 s long and can include neighbouring runs; for a
dropout (hundreds of J missing) the order of magnitude is clear nevertheless.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

try:
    import polars as pl
except ImportError:  # pragma: no cover – hint for calls with the wrong Python
    sys.exit(
        "polars is missing – start with metrion's Python: /opt/metrion/.venv/bin/python scripts/metrion_diagnose.py …"
    )

DYNAMIC = ("cpu_dynamic_j", "dram_dynamic_j", "gpu_dynamic_j")
STAMP = re.compile(r"_(\d{4}_\d{2}_\d{2}_\d{2}_\d{2}_\d{2})$")


def campaigns(data: dict) -> list[tuple[str, dict]]:
    if "candidates" in data:  # optimize
        out = [("measurement", data.get("measurement"))]
        out.append(("combination", data.get("combination_measurement")))
        return [(n, m) for n, m in out if m and m.get("runs")]
    return [("measurement", data)] if data.get("runs") else []


def utc_naive(iso: str) -> datetime:
    t = datetime.fromisoformat(iso)
    return (t.astimezone(UTC) if t.tzinfo else t).replace(tzinfo=None)


def pick_live(result_dir: Path, measurement: dict, explicit: str | None) -> Path | None:
    if explicit:
        return Path(explicit)
    first = min(utc_naive(r["t_start"]) for r in measurement["runs"] if r.get("t_start"))
    found = []
    for p in (result_dir / "metrion").glob("energy_*"):
        m = STAMP.search(p.name)
        if m:
            stamp = datetime.strptime(m.group(1), "%Y_%m_%d_%H_%M_%S")
            if stamp <= first:
                found.append((stamp, p))
    return max(found)[1] if found else None


def load_rows(live: Path) -> pl.DataFrame:
    files = sorted(live.glob("*.parquet")) if live.is_dir() else [live]
    if not files:
        sys.exit(f"No Parquet files in {live}")
    df = pl.concat([pl.read_parquet(f) for f in files], how="diagonal_relaxed")
    for c in DYNAMIC:
        if c not in df.columns:
            df = df.with_columns(pl.lit(0.0).alias(c))
    return df.with_columns(pl.sum_horizontal(*DYNAMIC).alias("dyn"))


def bench_pattern(measurement: dict) -> str:
    cmd = (measurement.get("config") or {}).get("benchmark", {}).get("command", "")
    parts = [p for p in cmd.split() if "/" in p or p.endswith(".py")]
    return parts[0] if parts else (cmd.split()[-1] if cmd else "")


def diagnose(name: str, measurement: dict, rows: pl.DataFrame) -> list[dict]:
    pattern = bench_pattern(measurement)
    print(f"\n== {name}: {len(measurement['runs'])} runs, benchmark pattern {pattern!r} ==")
    header = f"{'Run':<5} {'Variant':<10} {'CPU s':>7} {'joule:code':>10} {'attributed':>11} {'no identity':>12} {'unknown':>9} {'bench elsewhere':>15}"
    print(header)
    print("-" * len(header))
    report = []
    for r in measurement["runs"]:
        if not (r.get("t_start") and r.get("t_end") and r.get("container_id")):
            continue
        t0, t1 = utc_naive(r["t_start"]), utc_naive(r["t_end"])
        win = rows.filter((pl.col("window_start") < t1) & (pl.col("window_end") > t0))
        cid = r["container_id"]
        cg = pl.col("cgroup_name")
        matched = win.filter(cg.is_not_null() & cg.str.contains(cid, literal=True))["dyn"].sum()
        no_ident = win.filter(cg.is_null())["dyn"].sum()
        unknown = win.filter(cg == "unknown")["dyn"].sum()
        elsewhere = win.filter(
            cg.is_not_null()
            & (cg != "unknown")
            & ~cg.str.contains(cid, literal=True)
            & pl.col("cmdline").is_not_null()
            & pl.col("cmdline").str.contains(pattern, literal=True)
        )
        other_j = elsewhere["dyn"].sum()
        value = r.get("value") if r.get("value") is not None else r.get("value_raw")
        mark = (
            "  ← dropout"
            if r.get("discarded") or (value is not None and r.get("cpu_s") and value / r["cpu_s"] < 2)
            else ""
        )
        print(
            f"{r['run_id']:<5} {r['variant']:<10} {r.get('cpu_s') or 0:7.1f} {value or 0:10.1f} {matched:11.1f} "
            f"{no_ident:12.1f} {unknown:9.1f} {other_j:15.1f}{mark}"
        )
        if mark and other_j:
            for cgroup, j in (
                elsewhere.group_by("cgroup_name")
                .agg(pl.col("dyn").sum())
                .sort("dyn", descending=True)
                .rows()[:3]
            ):
                print(f"{'':>17}Benchmark in cgroup {cgroup}: {j:.1f} J")
        report.append(
            {"run": r["run_id"], "variant": r["variant"], "outage": bool(mark), "matched": matched,
             "no_identity": no_ident, "unknown": unknown, "elsewhere": other_j}
        )  # fmt: skip
    return report


def conclusion(report: list[dict]) -> None:
    outages = [x for x in report if x["outage"]]
    print(f"\nDropouts: {len(outages)} of {len(report)} runs.")
    if not outages:
        return
    big = lambda key: sum(1 for x in outages if x[key] > 50)  # noqa: E731
    if big("no_identity"):
        print(
            f"- {big('no_identity')}× the energy lies in rows without identity: metrion did not know the thread yet\n"
            "  when the window was written. The analysis of the raw data (monitor_*) knows all threads:\n"
            "  joulecode reanalyze <directory> --raw"
        )
    if big("unknown"):
        print(
            f"- {big('unknown')}× the energy lies with cgroup 'unknown': metrion could not resolve the process's cgroup ID\n"
            "  when it first saw it and keeps 'unknown' permanently (logical_entity_registry._cgroup_name)."
        )
    if big("elsewhere"):
        print(
            f"- {big('elsewhere')}× the benchmark ran in another cgroup according to metrion (see above) – the process\n"
            "  was probably recorded before it moved into the container cgroup."
        )
    if not (big("no_identity") or big("unknown") or big("elsewhere")):
        print(
            "- The energy is missing entirely from the live result. Possible cause: measurement rows arrived after the cutoff.\n"
            "  Check: journalctl -u joulecode-metrion | grep 'arrived after'"
        )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dir", help="result directory of joulecode measure/optimize")
    ap.add_argument("--live", help="live result (energy_…) instead of the archived one")
    args = ap.parse_args()
    result_dir = Path(args.dir)
    data = json.loads((result_dir / "result.json").read_text(encoding="utf-8"))
    report = []
    for name, m in campaigns(data):
        live = pick_live(result_dir, m, args.live)
        if live is None:
            print(f"{name}: no archived live result under {result_dir}/metrion – specify one with --live.")
            continue
        print(f"{name}: {live}")
        report += diagnose(name, m, load_rows(live))
    conclusion(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
