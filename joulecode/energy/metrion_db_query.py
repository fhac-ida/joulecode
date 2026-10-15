"""Helper script: runs with metrion's Python (its own venv), NOT with joule:code.

Reads a metrion DuckDB file via metrion's own analysis
(`execute_queries`) and sums up per run the energy of all threads whose
cgroup path (`resource_group_name`) contains the container ID.

    python metrion_db_query.py --db monitor.db --runs runs.json

runs.json: [{"run_id": "...", "container_id": "...", "t_start": ISO, "t_end": ISO}, …]
Output (stdout, one line): JOULECODE_JSON{"r001": {"cpu_active_j": …, …}, …}
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

COMPONENTS = ("cpu", "dram", "gpu")
MARGIN = timedelta(seconds=120)


def _naive_utc(text: str) -> datetime:
    ts = datetime.fromisoformat(text)
    return ts.astimezone(UTC).replace(tzinfo=None) if ts.tzinfo else ts


def summarize(frames, runs: list[dict]) -> dict:
    """frames: (cpu_df, dram_df, gpu_df) with columns resource_group_name, active/idle/total_energy_j."""
    out = {}
    for run in runs:
        cid = run["container_id"]
        sums = {}
        for comp, df in zip(COMPONENTS, frames, strict=True):
            if df is None or df.is_empty() or "resource_group_name" not in df.columns:
                continue
            rows = df.filter(df["resource_group_name"].fill_null("").str.contains(cid, literal=True))
            for kind in ("active", "idle", "total"):
                col = f"{kind}_energy_j"
                if col in rows.columns:
                    sums[f"{comp}_{kind}_j"] = float(rows[col].fill_null(0).sum())
            sums[f"{comp}_rows"] = rows.height
        out[run["run_id"]] = sums
    return out


def check_database(engine, start: datetime, end: datetime) -> None:
    """Clear error message instead of an SQL error if metrion has not written anything."""
    from sqlalchemy import inspect, text

    tables = set(inspect(engine).get_table_names())
    if "measurement" not in tables:
        raise SystemExit(
            "JOULECODE_ERROR: the metrion database contains no table 'measurement' – metrion has not written any "
            f"measurement data (existing tables: {sorted(tables) or 'none'}). "
            "Check with: journalctl -u joulecode-metrion -n 100"
        )
    with engine.connect() as conn:
        n = conn.execute(
            # Same form as in metrion's own queries (timestamp as text, UTC)
            text("SELECT count(*) FROM measurement WHERE start_ts >= :a AND start_ts <= :b"),
            {"a": start.strftime("%Y-%m-%d %H:%M:%S.%f"), "b": end.strftime("%Y-%m-%d %H:%M:%S.%f")},
        ).scalar()
    if not n:
        raise SystemExit(
            f"JOULECODE_ERROR: no metrion measurements between {start} and {end} UTC. "
            "Was metrion running during the measurement? Check with: journalctl -u joulecode-metrion -n 100"
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--runs", required=True)
    args = parser.parse_args()

    from metrion.energy_attribution.energy_attribution_cli import connect_to_db, execute_queries

    runs = json.loads(Path(args.runs).read_text())
    start = min(_naive_utc(r["t_start"]) for r in runs) - MARGIN
    end = max(_naive_utc(r["t_end"]) for r in runs) + MARGIN
    engine = connect_to_db(Path(args.db), "duckdb")
    if engine is None:
        raise SystemExit(f"Database not readable: {args.db}")
    if hasattr(engine, "connect"):  # real SQLAlchemy engine (in tests possibly a fake)
        check_database(engine, start, end)
    frames = execute_queries(engine, start, end, filter="", force=True)
    if frames is None:
        raise SystemExit("metrion analysis returned no data.")
    print("JOULECODE_JSON" + json.dumps(summarize(frames, runs)))


if __name__ == "__main__":
    main()
