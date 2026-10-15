"""Holdout tests for editdist: behaviour the visible tests do not check."""

import itertools

from editdist import closest, levenshtein, suggestions


def test_levenshtein_properties():
    words = ["", "a", "ab", "ba", "abc", "cab", "abcd"]
    for x, y in itertools.product(words, repeat=2):
        assert levenshtein(x, y) == levenshtein(y, x)
        assert levenshtein(x, y) <= max(len(x), len(y))
        assert (levenshtein(x, y) == 0) == (x == y)


def test_levenshtein_unicode_and_case():
    assert levenshtein("café", "cafe") == 1
    assert levenshtein("Straße", "Strasse") == 2
    assert levenshtein("ABC", "abc") == 3


def test_closest_single_and_exact():
    assert closest("zzz", ["only"]) == "only"
    assert closest("bb", ["aa", "bb", "bb"]) == "bb"


def test_suggestions_duplicates_and_zero_distance():
    vocab = ["ab", "ac", "ab", "zz"]
    assert suggestions("ab", vocab, 1) == ["ab", "ab", "ac"]
    assert suggestions("ab", vocab, 0) == ["ab", "ab"]
    assert suggestions("ab", [], 2) == []


def test_vocabulary_is_not_modified():
    vocab = ["b", "a", "c"]
    closest("a", vocab)
    suggestions("a", vocab, 1)
    assert vocab == ["b", "a", "c"]
