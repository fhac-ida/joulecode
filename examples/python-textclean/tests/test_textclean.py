from textclean import dedupe, join_lines, normalize_names, top_scores, word_counts


def test_dedupe_keeps_first_occurrence_order():
    assert dedupe(["b", "a", "b", "c", "a"]) == ["b", "a", "c"]
    assert dedupe(["A", "a"]) == ["A", "a"]


def test_normalize_names():
    assert normalize_names(["  anna  maria ", "BERND", "o'neil"]) == ["Anna Maria", "Bernd", "O'neil"]


def test_word_counts():
    counts = word_counts("The dog the cat THE")
    assert counts == {"the": 3, "dog": 1, "cat": 1}
    assert list(counts) == ["the", "dog", "cat"]


def test_top_scores_ties_keep_input_order():
    records = [("anna", 3), ("bernd", 5), ("clara", 3), ("dora", 5), ("emil", 1)]
    assert top_scores(records, 3) == [("bernd", 5), ("dora", 5), ("anna", 3)]
    assert top_scores(records, 10)[-1] == ("emil", 1)


def test_join_lines():
    assert join_lines(["a", "b"]) == "a\nb\n"
