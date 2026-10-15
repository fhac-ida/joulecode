from wordstats import frequent_words, report, unique_words

TEXT = "The dog and a cat and the bird and THE fish"


def test_unique_words_keeps_first_occurrence_order():
    assert unique_words(TEXT) == ["the", "dog", "and", "a", "cat", "bird", "fish"]


def test_unique_words_empty():
    assert unique_words("") == []


def test_frequent_words_counts_and_tie_break():
    assert frequent_words(TEXT, top=3) == [("and", 3), ("the", 3), ("a", 1)]


def test_frequent_words_top_larger_than_vocabulary():
    assert len(frequent_words("a b a", top=10)) == 2


def test_report_format():
    assert report("b a b", top=2) == "Total words: 3\nDistinct words: 2\n  b: 2\n  a: 1\n"
