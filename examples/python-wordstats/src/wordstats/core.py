"""Text statistics (original variant with typical inefficiencies)."""


def unique_words(text: str) -> list[str]:
    """All words in order of their first occurrence, lower-cased."""
    seen = []
    for word in text.lower().split():
        if word not in seen:  # membership check on a list: O(n)
            seen.append(word)
    return seen


def frequent_words(text: str, top: int = 10) -> list[tuple[str, int]]:
    """The `top` most frequent words with their count, ties sorted alphabetically."""
    words = text.lower().split()
    counts = []
    for word in unique_words(text):
        n = 0
        for other in words:  # nested loop: O(n*m)
            if other == word:
                n += 1
        counts.append((word, n))
    counts = sorted(counts, key=lambda wc: (-wc[1], wc[0]))
    return counts[:top]


def report(text: str, top: int = 5) -> str:
    """Short text report."""
    out = ""
    out += f"Total words: {len(text.split())}\n"
    out += f"Distinct words: {len(unique_words(text))}\n"
    for word, n in frequent_words(text, top):
        out += f"  {word}: {n}\n"  # string concatenation in a loop
    return out
