"""Benchmark: fixed, deterministic amount of work (BENCH_ROUNDS scales the run time)."""

import os
import random
import time

from editdist import closest, suggestions

ROUNDS = int(os.environ.get("BENCH_ROUNDS", "42"))
rng = random.Random(11)
letters = "abcdefghij"
vocabulary = ["".join(rng.choice(letters) for _ in range(rng.randint(4, 7))) for _ in range(60)]
queries = ["".join(rng.choice(letters) for _ in range(rng.randint(4, 7))) for _ in range(6)]

start = time.perf_counter()
checksum = 0
for _ in range(ROUNDS):
    for q in queries:
        checksum += len(closest(q, vocabulary))
        checksum += len(suggestions(q, vocabulary, 3))
print(f"rounds={ROUNDS} checksum={checksum} seconds={time.perf_counter() - start:.2f}")
