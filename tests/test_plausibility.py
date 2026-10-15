"""Detecting measurement dropouts: energy that does not match the measured CPU time."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from joulecode.cli import _pick_metrion_run
from joulecode.config import ProjectConfig
from joulecode.energy.base import Reading
from joulecode.protocol import MeasurementResult, flag_implausible, reevaluate

ENERGY = {"name": "metrion", "quantity": "Energy", "is_energy": True}


def _runs(spec: dict[str, list[tuple[float, float]]]) -> list[dict]:
    """spec: {variant: [(cpu_s, joule), …]} → run records, the first per variant is warmup."""
    runs, n = [], 0
    for variant, values in spec.items():
        for i, (cpu, joule) in enumerate(values):
            n += 1
            runs.append(
                {"run_id": f"r{n:03d}", "variant": variant, "warmup": i == 0, "cpu_s": cpu, "wall_s": cpu,
                 "value": joule, "unit": "J", "components": {}, "note": None}
            )  # fmt: skip
    return runs


def _result(spec, backend=ENERGY) -> MeasurementResult:
    return MeasurementResult(
        status="ok",
        started_at="2026-10-07T12:00:00+00:00",
        backend=dict(backend),
        variants=[{"name": v} for v in spec],
        runs=_runs(spec),
        config={"test": {"command": "pytest"}, "benchmark": {"command": "python b.py", "min_duration_s": 0}},
    )


# Observed on the measurement machine: ~21 J per CPU second, dropouts with ~1.5 J.
SPEC = {
    "baseline": [(22.7, 472.0)] + [(22.7, 480.0 + i) for i in range(8)] + [(22.7, 1.55)],
    "c2": [(4.6, 95.5), (4.6, 1.4)] + [(4.6, 97.0 + i * 0.5) for i in range(9)],
}


def test_outages_are_flagged_and_original_value_kept():
    r = _result(SPEC)
    assert flag_implausible(r) == 2
    bad = [x for x in r.runs if x.get("discarded")]
    assert {x["variant"] for x in bad} == {"baseline", "c2"}
    assert all(x["value"] is None and x["value_raw"] < 2 for x in bad)
    assert "discarded" in bad[0]["note"] and "expected" in bad[0]["note"]


def test_variants_with_different_power_density_are_not_flagged():
    # c3 computes “hotter” (40 J/s instead of 21 J/s) – that is not a dropout
    spec = {
        "baseline": [(10.0, 210.0 + i) for i in range(6)],
        "c3": [(5.0, 200.0 + i) for i in range(6)],
    }
    r = _result(spec)
    assert flag_implausible(r) == 0


def test_variant_made_mostly_of_outages_uses_campaign_reference():
    spec = {
        "baseline": [(10.0, 210.0 + i) for i in range(6)],
        "c2": [(5.0, 1.0), (5.0, 1.1), (5.0, 1.2), (5.0, 105.0)],
    }
    r = _result(spec)
    assert flag_implausible(r) == 3
    assert [x["value"] for x in r.runs if x["variant"] == "c2"][-1] == 105.0


def test_cpu_time_backend_is_never_flagged():
    r = _result(SPEC, backend={"name": "cgroup", "is_energy": False})
    assert flag_implausible(r) == 0


def test_reevaluate_excludes_outages_from_statistics():
    r = _result(SPEC)
    readings = {x["run_id"]: Reading(x["value"], "J") for x in r.runs}
    cfg = ProjectConfig.model_validate(r.config)
    reevaluate(r, readings, cfg)
    assert r.summary["baseline"]["primary"]["n"] == 8  # 9 counted minus 1 dropout
    assert r.summary["baseline"]["primary"]["cv"] < 0.02
    prim = r.comparison["c2"]["primary"]
    assert prim["verdict"] == "better"
    assert prim["ci_high"] < -0.7  # without the filter the CI reached far above 0
    assert any("discarded" in w for w in r.warnings)
    assert not any("without a measurement value" in w for w in r.warnings)
    # analysing again resets the marking and marks anew
    reevaluate(r, {x["run_id"]: Reading(x.get("value_raw", x["value"]), "J") for x in r.runs}, cfg)
    assert sum(1 for x in r.runs if x.get("discarded")) == 2


def test_pick_raw_run_when_requested():
    class M:
        runs = [{"t_start": datetime(2026, 10, 7, 12, 10, 14, tzinfo=UTC).isoformat()}]

    paths = [Path("/x/energy_2026_10_07_12_09_55"), Path("/x/monitor_2026_10_07_12_09_55")]
    assert _pick_metrion_run(paths, M()).name.startswith("energy_")
    assert _pick_metrion_run(paths, M(), prefer_raw=True).name.startswith("monitor_")
