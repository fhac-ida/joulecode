"""Benchmark: fixed, deterministic amount of work (BENCH_ROUNDS scales the run time)."""

import os
import random
import time

from textclean import dedupe, join_lines, normalize_names, top_scores, word_counts

ROUNDS = int(os.environ.get("BENCH_ROUNDS", "34"))
rng = random.Random(3)
syllables = ["an", "ber", "cla", "do", "em", "fri", "ga", "hel", "ir", "jo", "ka", "lu"]


def word() -> str:
    return "".join(rng.choice(syllables) for _ in range(rng.randint(1, 3)))


items = [word() + word() for _ in range(6000)]
names = ["  ".join(word().upper() for _ in range(rng.randint(1, 4))) for _ in range(3000)]
text = " ".join(word() for _ in range(30000))
records = [(word(), rng.randrange(100)) for _ in range(3000)]
lines = [word() * 3 for _ in range(10000)]

start = time.perf_counter()
checksum = 0
for _ in range(ROUNDS):
    checksum += len(dedupe(items))
    checksum += sum(len(n) for n in normalize_names(names))
    checksum += sum(word_counts(text).values())
    checksum += sum(s for _, s in top_scores(records, 600))
    checksum += len(join_lines(lines))
print(f"rounds={ROUNDS} checksum={checksum} seconds={time.perf_counter() - start:.2f}")
