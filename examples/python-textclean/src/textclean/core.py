"""Text cleaning (original variant with typical inefficiencies).

Every function has a property that an obvious optimisation easily
violates (order, ties, upper/lower case, empty input,
unchanged input).
"""


def dedupe(items: list[str]) -> list[str]:
    """Remove duplicates; the first occurrence stays, and so does the order."""
    result = []
    for item in items:
        if item not in result:
            result.append(item)
    return result


def normalize_names(names: list[str]) -> list[str]:
    """Trim and collapse whitespace, every word with a capital initial letter."""
    result = []
    for name in names:
        words = []
        for word in name.split():
            words.append(word[:1].upper() + word[1:].lower())
        cleaned = ""
        for i, word in enumerate(words):
            if i > 0:
                cleaned += " "
            cleaned += word
        result.append(cleaned)
    return result


def word_counts(text: str) -> dict[str, int]:
    """Frequency per word (compared with str.lower), in order of first occurrence."""
    words = text.lower().split()
    counts = {}
    for word in words:
        if word not in counts:
            counts[word] = words.count(word)  # counts the whole list for every new word
    return counts


def top_scores(records: list[tuple[str, int]], n: int) -> list[tuple[str, int]]:
    """The n entries with the highest score; on a tie the input order applies."""
    remaining = list(records)
    result = []
    while remaining and len(result) < n:
        best = 0
        for i in range(1, len(remaining)):
            if remaining[i][1] > remaining[best][1]:
                best = i
        result.append(remaining.pop(best))
    return result


def join_lines(lines: list[str]) -> str:
    """Concatenate every line with a trailing line break."""
    out = ""
    for line in lines:
        out = out + line + "\n"
    return out
