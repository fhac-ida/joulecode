"""Checksums and compression.

This variant is already efficient: the work is done by hashlib, zlib and
sorted in C. joule:code should not find any noteworthy saving here.
"""

import hashlib
import zlib
from collections import Counter


def block_digests(data: bytes, block_size: int = 1 << 16) -> list[str]:
    """SHA-256 (hex) per block of fixed size; the last block may be shorter."""
    view = memoryview(data)
    return [hashlib.sha256(view[i : i + block_size]).hexdigest() for i in range(0, len(data), block_size)]


def duplicate_blocks(digests: list[str]) -> list[str]:
    """Checksums that occur more than once, in order of their first occurrence."""
    return [d for d, n in Counter(digests).items() if n > 1]


def compress_ratio(data: bytes, level: int = 6) -> float:
    """Ratio of compressed/uncompressed size (zlib); empty data yields 1.0."""
    if not data:
        return 1.0
    return len(zlib.compress(data, level)) / len(data)


def sorted_sizes(sizes: list[int]) -> list[int]:
    """Sizes sorted in descending order."""
    return sorted(sizes, reverse=True)
