"""Benchmark: fixed amount of work; consists almost only of waiting time (BENCH_ROUNDS scales it)."""

import os
import time

from iowait import total_price

ROUNDS = int(os.environ.get("BENCH_ROUNDS", "25"))
items = [f"item-{i}" for i in range(20)]

start = time.perf_counter()
checksum = 0.0
for _ in range(ROUNDS):
    checksum += total_price(items)
print(f"rounds={ROUNDS} checksum={checksum:.2f} seconds={time.perf_counter() - start:.2f}")
