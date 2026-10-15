"""Statistics for comparing original vs. candidate.

Energy measurement series are rarely normally distributed (outliers from background
load, turbo, thermals). Hence: median instead of mean, Mann-Whitney U instead of
t-test, and a bootstrap confidence interval for the relative change of the
medians. A candidate only counts as better if the test is significant AND
the whole interval lies below 0.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

import numpy as np
from scipy.stats import mannwhitneyu


@dataclass
class Summary:
    n: int
    median: float
    mean: float
    std: float
    cv: float  # coefficient of variation std/mean
    min: float
    max: float

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Comparison:
    rel_change: float  # median candidate / median original − 1 (negative = less)
    ci_low: float
    ci_high: float
    p_value: float
    verdict: str  # "better" | "worse" | "no significant difference"

    def to_dict(self) -> dict:
        return asdict(self)


def summarize(values: list[float]) -> Summary:
    a = np.asarray(values, dtype=float)
    mean = float(a.mean())
    std = float(a.std(ddof=1)) if len(a) > 1 else 0.0
    return Summary(
        n=len(a),
        median=float(np.median(a)),
        mean=mean,
        std=std,
        cv=std / mean if mean else float("inf"),
        min=float(a.min()),
        max=float(a.max()),
    )


def compare(
    baseline: list[float],
    candidate: list[float],
    *,
    alpha: float = 0.05,
    n_boot: int = 10_000,
    seed: int | None = 0,
) -> Comparison:
    b = np.asarray(baseline, dtype=float)
    c = np.asarray(candidate, dtype=float)
    rng = np.random.default_rng(seed)

    rel = float(np.median(c) / np.median(b) - 1)
    boot_b = np.median(rng.choice(b, size=(n_boot, len(b)), replace=True), axis=1)
    boot_c = np.median(rng.choice(c, size=(n_boot, len(c)), replace=True), axis=1)
    with np.errstate(divide="ignore", invalid="ignore"):
        ratios = boot_c / boot_b - 1
    ratios = ratios[np.isfinite(ratios)]
    lo, hi = (float(x) for x in np.quantile(ratios, [alpha / 2, 1 - alpha / 2]))

    p = float(mannwhitneyu(b, c, alternative="two-sided").pvalue)
    if p < alpha and hi < 0:
        verdict = "better"
    elif p < alpha and lo > 0:
        verdict = "worse"
    else:
        verdict = "no significant difference"
    return Comparison(rel, lo, hi, p, verdict)


def early_decision(
    baseline: list[float],
    candidate: list[float],
    *,
    alpha: float,
    precision: float,
    seed: int | None = 0,
) -> str | None:
    """Is the comparison already decided? "better"/"worse"/"equal" or None (keep measuring).

    Called after every block; `alpha` must therefore be corrected for the number of interim
    looks (see protocol.stop_alpha), otherwise repeated looking increases the error
    rate. “Equal” means: the whole CI lies within ±precision.
    """
    if len(baseline) < 2 or len(candidate) < 2:
        return None
    cmp = compare(baseline, candidate, alpha=alpha, n_boot=2000, seed=seed)
    if cmp.verdict in ("better", "worse"):
        return cmp.verdict
    if -precision < cmp.ci_low and cmp.ci_high < precision:
        return "equal"
    return None
