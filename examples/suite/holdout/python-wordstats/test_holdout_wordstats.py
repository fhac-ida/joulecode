"""Holdout tests for wordstats: behaviour the visible tests do not check."""

from wordstats import frequent_words, report, unique_words


def test_lower_not_casefold():
    # casefold() would merge "straße" and "strasse"
    assert unique_words("Straße STRASSE") == ["straße", "strasse"]
    assert frequent_words("Straße STRASSE straße", 5) == [("straße", 2), ("strasse", 1)]


def test_punctuation_and_whitespace_are_kept_as_is():
    assert unique_words("Dog, dog\tdog.\nDOG,") == ["dog,", "dog", "dog."]


def test_frequent_words_edges():
    assert frequent_words("", 3) == []
    assert frequent_words("a b c", 0) == []
    assert frequent_words("c b a b c", 10) == [("b", 2), ("c", 2), ("a", 1)]


def test_report_empty_and_top_zero():
    assert report("") == "Total words: 0\nDistinct words: 0\n"
    assert report("x y x", top=0) == "Total words: 3\nDistinct words: 2\n"
