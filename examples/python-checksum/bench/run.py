"""Benchmark: fixed, deterministic amount of work (BENCH_ROUNDS scales the run time)."""

import os
import random
import time

from checksum import block_digests, compress_ratio, duplicate_blocks, sorted_sizes

ROUNDS = int(os.environ.get("BENCH_ROUNDS", "80"))
rng = random.Random(21)
chunks = [rng.randbytes(1 << 16) for _ in range(64)]
data = b"".join(rng.choice(chunks) for _ in range(256))  # 16 MiB with repeated blocks
text = (" ".join(str(rng.randrange(1000)) for _ in range(400_000))).encode()
sizes = [rng.randrange(1 << 30) for _ in range(300_000)]

start = time.perf_counter()
checksum = 0.0
for _ in range(ROUNDS):
    digests = block_digests(data)
    checksum += len(duplicate_blocks(digests))
    checksum += compress_ratio(text)
    checksum += sorted_sizes(sizes)[0] % 1000
print(f"rounds={ROUNDS} checksum={checksum:.6f} seconds={time.perf_counter() - start:.2f}")
