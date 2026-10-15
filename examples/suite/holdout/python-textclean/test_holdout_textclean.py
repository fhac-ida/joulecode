"""Holdout tests for textclean: behaviour the visible tests do not check."""

from textclean import dedupe, join_lines, normalize_names, top_scores, word_counts


def test_inputs_are_not_modified():
    items = ["b", "a", "b"]
    names = ["  x  y ", "Z"]
    records = [("a", 1), ("b", 2)]
    lines = ["l1", "l2"]
    dedupe(items)
    normalize_names(names)
    top_scores(records, 1)
    join_lines(lines)
    assert items == ["b", "a", "b"]
    assert names == ["  x  y ", "Z"]
    assert records == [("a", 1), ("b", 2)]
    assert lines == ["l1", "l2"]


def test_results_are_fresh_lists():
    items = ["a", "b"]
    out = dedupe(items)
    out.append("c")
    assert items == ["a", "b"]
    records = [("a", 1)]
    assert top_scores(records, 5) is not records


def test_word_counts_uses_lower_not_casefold():
    # casefold() would merge "straße" and "strasse"
    assert word_counts("Straße STRASSE straße") == {"straße": 2, "strasse": 1}


def test_word_counts_empty_and_whitespace():
    assert word_counts("") == {}
    assert word_counts(" \t\n ") == {}
    assert word_counts("a\tb\na") == {"a": 2, "b": 1}


def test_join_lines_empty_and_empty_lines():
    assert join_lines([]) == ""
    assert join_lines([""]) == "\n"
    assert join_lines(["a", "", "b"]) == "a\n\nb\n"


def test_normalize_names_edges():
    assert normalize_names([]) == []
    assert normalize_names(["   "]) == [""]
    assert normalize_names(["éLISE ÜBEL"]) == ["Élise Übel"]
    assert normalize_names(["mc\tdonald\nx"]) == ["Mc Donald X"]


def test_top_scores_edges():
    records = [("a", 2), ("b", 2), ("c", 2)]
    assert top_scores(records, 2) == [("a", 2), ("b", 2)]
    assert top_scores(records, 0) == []
    assert top_scores([], 3) == []
    assert top_scores([("x", -1), ("y", -5), ("z", -1)], 3) == [("x", -1), ("z", -1), ("y", -5)]


def test_dedupe_edges():
    assert dedupe([]) == []
    assert dedupe(["1", "01", "1"]) == ["1", "01"]
