from editdist import closest, levenshtein, suggestions


def test_levenshtein():
    assert levenshtein("kitten", "sitting") == 3
    assert levenshtein("", "abc") == 3
    assert levenshtein("abc", "") == 3
    assert levenshtein("same", "same") == 0
    assert levenshtein("flaw", "lawn") == 2


def test_closest_tie_prefers_earlier():
    assert closest("hat", ["cat", "bat", "hut"]) == "cat"
    assert closest("hut", ["cat", "bat", "hut"]) == "hut"


def test_suggestions_sorted_by_distance_then_vocabulary():
    vocab = ["hat", "cat", "hand", "bat", "hint"]
    assert suggestions("hat", vocab, 1) == ["hat", "cat", "bat"]
    assert suggestions("xyz", vocab, 1) == []
