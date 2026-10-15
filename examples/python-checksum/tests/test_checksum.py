import hashlib

from checksum import block_digests, compress_ratio, duplicate_blocks, sorted_sizes


def test_block_digests():
    data = b"a" * 10 + b"b" * 5
    assert block_digests(data, 10) == [
        hashlib.sha256(b"a" * 10).hexdigest(),
        hashlib.sha256(b"b" * 5).hexdigest(),
    ]
    assert block_digests(b"", 10) == []


def test_duplicate_blocks():
    assert duplicate_blocks(["x", "y", "x", "z", "y", "x"]) == ["x", "y"]


def test_compress_ratio():
    assert compress_ratio(b"") == 1.0
    assert compress_ratio(b"a" * 1000) < 0.05
    assert round(compress_ratio(bytes(range(256)) * 4, 6), 6) == round(
        len(__import__("zlib").compress(bytes(range(256)) * 4, 6)) / 1024, 6
    )


def test_sorted_sizes():
    assert sorted_sizes([3, 1, 2]) == [3, 2, 1]
