"""Finding hotspots: run the benchmark once under cProfile in the container.

metrion measures per process, not per function. So that the optimiser starts at the
right places, a profiling run determines the functions with the
largest share of self time. The analysis happens in the container (same
Python version as when profiling); only JSON comes out.
"""

from __future__ import annotations

import json
import os
import re
import shlex
from dataclasses import asdict, dataclass, field
from pathlib import Path

from joulecode.config import ProjectConfig
from joulecode.container import Runner
from joulecode.pycode import function_at_line

PROFILE_OUT = "/tmp/joulecode.prof"

_STATS_SCRIPT = r"""
import json, os, pstats, sys
st = pstats.Stats(sys.argv[1])
root = os.getcwd()
excluded = json.loads(sys.argv[2])
rows = []
for (fn, line, name), (cc, nc, tt, ct, callers) in st.stats.items():
    path = os.path.abspath(fn)
    if not path.startswith(root + os.sep):
        continue
    rel = os.path.relpath(path, root).replace(os.sep, "/")
    if any(rel == e or rel.startswith(e.rstrip("/") + "/") for e in excluded):
        continue
    rows.append({"file": rel, "line": line, "function": name, "ncalls": nc, "tottime": tt, "cumtime": ct})
print("JOULECODE_JSON" + json.dumps({"total_s": st.total_tt, "functions": rows}))
"""


@dataclass
class Hotspot:
    file: str  # relative to the repository
    qualname: str
    line: int
    end_line: int
    ncalls: int
    tottime: float
    cumtime: float
    share: float  # self time / total time of the profile
    code: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ProfileResult:
    ok: bool
    profiler: str = "cProfile"
    total_s: float = 0.0
    hotspots: list[Hotspot] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "profiler": self.profiler,
            "total_s": self.total_s,
            "hotspots": [h.to_dict() for h in self.hotspots],
            "error": self.error,
        }


def profile_command(command: str, out: str = PROFILE_OUT) -> str:
    """Rewrite `python [flags] script.py args` or `python -m mod` to use cProfile."""
    parts = shlex.split(command)
    if not parts or not re.fullmatch(r"python(3(\.\d+)?)?", os.path.basename(parts[0])):
        raise ValueError(
            f"Benchmark command {command!r} is not a direct Python call. "
            "Please specify profile.command in joulecode.yml."
        )
    rest = parts[1:]
    flags = []
    while rest and rest[0].startswith("-") and rest[0] != "-m":
        flags.append(rest.pop(0))
    return shlex.join([parts[0], *flags, "-m", "cProfile", "-o", out, *rest])


def _parse_stats(output: str) -> dict:
    for line in output.splitlines():
        if line.startswith("JOULECODE_JSON"):
            return json.loads(line[len("JOULECODE_JSON") :])
    raise ValueError(f"No profile analysis in the output:\n{output[-2000:]}")


def select_hotspots(stats: dict, repo: Path, *, limit: int, min_share: float) -> list[Hotspot]:
    total = stats["total_s"] or 1e-9
    hotspots: list[Hotspot] = []
    for row in sorted(stats["functions"], key=lambda r: r["tottime"], reverse=True):
        share = row["tottime"] / total
        if share < min_share or len(hotspots) >= limit:
            break
        path = repo / row["file"]
        if path.suffix != ".py" or not path.is_file():
            continue
        source = path.read_text(encoding="utf-8")
        span = function_at_line(source, row["line"])
        if span is None:  # module level, lambda or similar
            continue
        lines = source.splitlines(keepends=True)
        hotspots.append(
            Hotspot(
                file=row["file"],
                qualname=span.qualname,
                line=span.start,
                end_line=span.end,
                ncalls=row["ncalls"],
                tottime=row["tottime"],
                cumtime=row["cumtime"],
                share=share,
                code=span.source(lines),
            )
        )
    return hotspots


def profile(cfg: ProjectConfig, runner: Runner, image: str, repo: Path, *, log=print) -> ProfileResult:
    try:
        command = cfg.profile.command or profile_command(cfg.benchmark.command)
    except ValueError as exc:
        return ProfileResult(ok=False, error=str(exc))
    env = {**cfg.benchmark.env, **cfg.profile.env, "JOULECODE_PROFILE_OUT": PROFILE_OUT}
    box = runner.start(image, cfg.limits)
    try:
        log("Profiling run …")
        res = box.exec(command, cfg.profile.timeout_s, env)
        if not res.ok:
            return ProfileResult(ok=False, error=f"Profiling run exit {res.exit_code}:\n{res.output[-2000:]}")
        script = f"python3 -c {shlex.quote(_STATS_SCRIPT)} {PROFILE_OUT} {shlex.quote(json.dumps(cfg.protected_paths))}"
        stats_res = box.exec(script, 120)
    finally:
        box.remove()
    try:
        stats = _parse_stats(stats_res.output)
    except (ValueError, json.JSONDecodeError) as exc:
        return ProfileResult(ok=False, error=str(exc))
    hotspots = select_hotspots(stats, repo, limit=cfg.optimize.hotspots, min_share=cfg.optimize.min_share)
    return ProfileResult(ok=True, total_s=stats["total_s"], hotspots=hotspots)
