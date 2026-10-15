"""Holdout tests for checksum: behaviour the visible tests do not check."""

import hashlib
import zlib

from checksum import block_digests, compress_ratio, duplicate_blocks, sorted_sizes


def test_block_digests_edges():
    data = bytes(range(100))
    assert block_digests(data, 1000) == [hashlib.sha256(data).hexdigest()]
    assert len(block_digests(data, 7)) == 15
    assert block_digests(data, 7)[-1] == hashlib.sha256(data[98:]).hexdigest()
    assert block_digests(bytearray(b"xy"), 1) == [
        hashlib.sha256(b"x").hexdigest(),
        hashlib.sha256(b"y").hexdigest(),
    ]


def test_default_block_size_is_64_kib():
    data = b"\x01" * ((1 << 16) + 1)
    assert len(block_digests(data)) == 2


def test_compress_ratio_respects_level():
    data = bytes(range(256)) * 50 + b"abc" * 3000
    for level in (1, 6, 9):
        assert compress_ratio(data, level) == len(zlib.compress(data, level)) / len(data)


def test_duplicate_blocks_order_and_empty():
    assert duplicate_blocks([]) == []
    assert duplicate_blocks(["b", "a", "a", "b"]) == ["b", "a"]


def test_sorted_sizes_leaves_input_untouched():
    sizes = [2, 9, 4]
    assert sorted_sizes(sizes) == [9, 4, 2]
    assert sizes == [2, 9, 4]
