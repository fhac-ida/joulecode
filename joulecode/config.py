"""Schema of the project file `joulecode.yml`.

The file lies in the root directory of the repository to be optimised and
describes how it is built, tested and put under load. Unknown
keys are rejected so that a typo does not silently leave the
default in place.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

CONFIG_NAMES = ("joulecode.yml", "joulecode.yaml")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BuildConfig(_Strict):
    dockerfile: str = "Dockerfile"
    context: str = "."


class CommandConfig(_Strict):
    command: str
    timeout_s: int = Field(300, gt=0)
    env: dict[str, str] = Field(default_factory=dict)


class BenchmarkConfig(CommandConfig):
    # Shorter runs cannot be measured reliably with RAPL/metrion (1 s sampling,
    # 10 s window); the report then warns.
    min_duration_s: float = Field(20.0, ge=0)


class LimitsConfig(_Strict):
    cpus: float = Field(1.0, gt=0)
    memory: str = "1g"
    # Fixed cores reduce measurement noise (e.g. "2" or "2-3").
    cpuset: str | None = None
    pids: int = Field(512, gt=0)


class MeasurementConfig(_Strict):
    # Maximum number of repetitions (blocks). With `adaptive`, measuring stops per candidate
    # earlier as soon as the result is clear, at the earliest after `min_repetitions`.
    repetitions: int = Field(10, ge=2)
    min_repetitions: int = Field(5, ge=2)
    adaptive: bool = True
    # Differences whose 95% CI lies entirely within ±precision count as “equal”
    # for early stopping – more runs would not change that.
    precision: float = Field(0.02, gt=0, lt=1)
    # If the back-end discarded runs (attribution dropout) and fewer than `min_repetitions`
    # valid values remain as a result, a second, short session tops up the measurement.
    topup: bool = True
    warmup: int = Field(1, ge=0)
    cooldown_s: float = Field(5.0, ge=0)
    alpha: float = Field(0.05, gt=0, lt=1)
    seed: int | None = None


class ProfileConfig(_Strict):
    # Python: if not specified, the benchmark command is automatically wrapped with cProfile
    # (works for `python script.py` and `python -m module`). For other commands
    # specify your own here that writes the profile to $JOULECODE_PROFILE_OUT.
    # PHP: the benchmark runs with Xdebug in profile mode (controlled via
    # XDEBUG_MODE/XDEBUG_CONFIG); a command of your own is rarely necessary.
    command: str | None = None
    # Additional variables only for the profiling run (e.g. smaller load).
    env: dict[str, str] = Field(default_factory=dict)
    timeout_s: int = Field(900, gt=0)
    # PHP only: shell command that installs Xdebug in the derived profiling image.
    # If not specified: pecl install xdebug (works for the official php images).
    setup: str | None = None


class OptimizeConfig(_Strict):
    # How many hotspot functions the optimiser works on.
    hotspots: int = Field(2, ge=1)
    # Functions with a smaller share of the total runtime are ignored.
    min_share: float = Field(0.05, ge=0, le=1)
    candidates_per_hotspot: int = Field(2, ge=1, le=5)
    # Repair attempts if a candidate fails the tests.
    repair_attempts: int = Field(1, ge=0, le=3)
    # Revision rounds after a veto of the maintainability reviewer.
    review_rounds: int = Field(1, ge=0, le=2)
    # Veto if the cyclomatic complexity of a function increases by more than this.
    max_complexity_increase: int = Field(3, ge=0)
    # Candidates for different hotspots complement each other. If for several hotspots
    # a candidate is significantly better, the combination of the respective best ones
    # is built, tested and checked in a second measurement.
    combine: bool = True


class ProjectConfig(_Strict):
    version: Literal[1] = 1
    # javascript and typescript use the same back-end (Node.js).
    language: Literal["python", "php", "javascript", "typescript", "java"] = "python"
    build: BuildConfig = Field(default_factory=BuildConfig)
    test: CommandConfig
    benchmark: BenchmarkConfig
    limits: LimitsConfig = Field(default_factory=LimitsConfig)
    measurement: MeasurementConfig = Field(default_factory=MeasurementConfig)
    profile: ProfileConfig = Field(default_factory=ProfileConfig)
    optimize: OptimizeConfig = Field(default_factory=OptimizeConfig)
    # Paths a candidate must not change. Before building they are
    # taken from the original – otherwise a candidate could "optimise" tests or
    # the benchmark instead of the code.
    protected_paths: list[str] = Field(default_factory=lambda: ["tests", "bench", *CONFIG_NAMES])

    @field_validator("protected_paths")
    @classmethod
    def _relative_only(cls, paths: list[str]) -> list[str]:
        for p in paths:
            if Path(p).is_absolute() or ".." in Path(p).parts:
                raise ValueError(f"protected_paths must be relative and inside the repository: {p!r}")
        return paths


def find_config(repo: Path) -> Path:
    for name in CONFIG_NAMES:
        candidate = repo / name
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(f"No joulecode.yml found in {repo}.")


def load_config(repo: Path) -> ProjectConfig:
    path = find_config(repo)
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return ProjectConfig.model_validate(data)
