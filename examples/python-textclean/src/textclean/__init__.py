"""Text cleaning – with traps for obvious but behaviour-changing optimisations."""

from textclean.core import dedupe, join_lines, normalize_names, top_scores, word_counts

__all__ = ["dedupe", "join_lines", "normalize_names", "top_scores", "word_counts"]
