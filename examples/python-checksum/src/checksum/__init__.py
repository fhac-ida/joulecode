"""Checksums and compression (negative control: already efficient, the work happens in C)."""

from checksum.core import block_digests, compress_ratio, duplicate_blocks, sorted_sizes

__all__ = ["block_digests", "compress_ratio", "duplicate_blocks", "sorted_sizes"]
