"""Benchmark: fixed, deterministic amount of work (BENCH_ROUNDS scales the run time)."""

import os
import random
import time
from datetime import datetime, timedelta

from logstats import endpoint_stats, parse_log, slowest_requests, summary_table

ROUNDS = int(os.environ.get("BENCH_ROUNDS", "26"))
LINES = int(os.environ.get("BENCH_LINES", "6000"))

rng = random.Random(99)
paths = ["/api/users", "/api/orders", "/health", "/static/app.js"] + [f"/api/v{i}/items" for i in range(30)]
t0 = datetime(2026, 10, 7, 8, 0, 0)
lines = []
for i in range(LINES):
    path = rng.choice(paths)
    if rng.random() < 0.4:
        path += f"/{rng.randrange(10_000)}"
    status = 500 if rng.random() < 0.03 else 200
    lines.append(
        f"{(t0 + timedelta(seconds=i)).isoformat()} {rng.choice(['GET', 'POST'])} {path} {status} "
        f"{rng.lognormvariate(3, 0.8):.1f}"
    )
text = "\n".join(lines)

start = time.perf_counter()
checksum = 0.0
for _ in range(ROUNDS):
    requests = parse_log(text)
    stats = endpoint_stats(requests)
    checksum += sum(s["p95_ms"] for s in stats.values())
    checksum += sum(r.duration_ms for r in slowest_requests(requests, 50))
    checksum += len(summary_table(text))
print(f"rounds={ROUNDS} checksum={checksum:.1f} seconds={time.perf_counter() - start:.2f}")
