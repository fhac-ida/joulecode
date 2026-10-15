"""Benchmark: fixed, deterministic amount of work.

The energy for exactly this work is measured. BENCH_ROUNDS scales the
run time (on the measurement machine, choose it so that a run takes >= 20 s).
"""

import os
import random
import time

from wordstats import frequent_words, report, unique_words

ROUNDS = int(os.environ.get("BENCH_ROUNDS", "40"))
VOCAB = int(os.environ.get("BENCH_VOCAB", "1500"))
WORDS = int(os.environ.get("BENCH_WORDS", "6000"))

rng = random.Random(1234)
vocabulary = [f"word{i}" for i in range(VOCAB)]
texts = [" ".join(rng.choice(vocabulary) for _ in range(WORDS)) for _ in range(5)]

start = time.perf_counter()
checksum = 0
for i in range(ROUNDS):
    text = texts[i % len(texts)]
    checksum += len(unique_words(text))
    checksum += sum(n for _, n in frequent_words(text, 20))
    checksum += len(report(text, 10))
print(f"rounds={ROUNDS} checksum={checksum} seconds={time.perf_counter() - start:.2f}")
