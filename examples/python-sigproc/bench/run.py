"""Benchmark: fixed, deterministic amount of work (BENCH_ROUNDS scales the run time)."""

import os
import time

import numpy as np

from sigproc import moving_average, pairwise_distances, peaks, zscore

ROUNDS = int(os.environ.get("BENCH_ROUNDS", "44"))
rng = np.random.default_rng(5)
signal = np.cumsum(rng.normal(size=100_000))
points = rng.normal(size=(300, 3))

start = time.perf_counter()
checksum = 0.0
for _ in range(ROUNDS):
    smooth = moving_average(signal, 40)
    checksum += float(smooth.sum())
    checksum += float(np.abs(zscore(signal)).sum())
    checksum += len(peaks(smooth, 0.0))
    checksum += float(pairwise_distances(points).sum())
print(f"rounds={ROUNDS} checksum={checksum:.6f} seconds={time.perf_counter() - start:.2f}")
