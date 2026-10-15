"""Edit distance, recursive and without a cache (original variant)."""


def levenshtein(a: str, b: str) -> int:
    """Minimum number of insertions, deletions and substitutions that turn a into b."""
    if not a:
        return len(b)
    if not b:
        return len(a)
    cost = 0 if a[-1] == b[-1] else 1
    return min(
        levenshtein(a[:-1], b) + 1,
        levenshtein(a, b[:-1]) + 1,
        levenshtein(a[:-1], b[:-1]) + cost,
    )


def closest(word: str, vocabulary: list[str]) -> str:
    """The word from `vocabulary` with the smallest distance; on a tie the earlier one."""
    best = vocabulary[0]
    best_d = levenshtein(word, best)
    for candidate in vocabulary[1:]:
        d = levenshtein(word, candidate)
        if d < best_d:
            best, best_d = candidate, d
    return best


def suggestions(word: str, vocabulary: list[str], max_distance: int = 2) -> list[str]:
    """All words with distance <= max_distance, by distance, then by vocabulary order."""
    scored = []
    for candidate in vocabulary:
        d = levenshtein(word, candidate)
        if d <= max_distance:
            scored.append((d, vocabulary.index(candidate), candidate))
    scored.sort()
    return [c for _, _, c in scored]
