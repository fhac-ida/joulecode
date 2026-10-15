"""Benchmark: fixed, deterministic amount of work (BENCH_ROUNDS scales the run time)."""

import os
import random
import time

from graphroute import Network

ROUNDS = int(os.environ.get("BENCH_ROUNDS", "18"))
NODES = int(os.environ.get("BENCH_NODES", "1500"))
EDGES = int(os.environ.get("BENCH_EDGES", "4500"))

rng = random.Random(7)
net = Network()
for i in range(1, NODES):  # connected: tree + random edges
    net.add_edge(i, rng.randrange(i))
while len(net.edges) < EDGES:
    a, b = rng.randrange(NODES), rng.randrange(NODES)
    if a != b:
        net.add_edge(a, b)
pairs = [(rng.randrange(NODES), rng.randrange(NODES)) for _ in range(30)]

start = time.perf_counter()
checksum = 0
for _ in range(ROUNDS):
    for a, b in pairs:
        checksum += len(net.shortest_path(a, b) or [])
    checksum += sum(net.degree_histogram().values())
print(f"rounds={ROUNDS} checksum={checksum} seconds={time.perf_counter() - start:.2f}")
