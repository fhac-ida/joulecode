package wordstats;

import static org.junit.jupiter.api.Assertions.assertEquals;

import java.util.List;
import java.util.Map;
import org.junit.jupiter.api.Test;

class WordStatsTest {

    private static final String TEXT = "The dog and a cat and the bird and THE fish";

    @Test
    void uniqueWordsKeepsFirstOccurrenceOrder() {
        assertEquals(List.of("the", "dog", "and", "a", "cat", "bird", "fish"), WordStats.uniqueWords(TEXT));
    }

    @Test
    void uniqueWordsOrderIsNotHashOrder() {
        // A HashSet orders by hash value – the order of first occurrence must be kept.
        assertEquals(List.of("zebra", "apple", "moon", "tree", "10", "9"),
                WordStats.uniqueWords("zebra apple moon zebra tree 10 9 apple"));
    }

    @Test
    void uniqueWordsEmpty() {
        assertEquals(List.of(), WordStats.uniqueWords(""));
    }

    @Test
    void frequentWordsCountsAndBreaksTiesAlphabetically() {
        assertEquals(List.of(Map.entry("and", 3), Map.entry("the", 3), Map.entry("a", 1)),
                WordStats.frequentWords(TEXT, 3));
    }

    @Test
    void frequentWordsTopLargerThanVocabulary() {
        assertEquals(2, WordStats.frequentWords("a b a", 10).size());
    }

    @Test
    void frequentWordsResultIsModifiable() {
        // Callers may keep using the list – do not return an unmodifiable view.
        WordStats.frequentWords("a b a", 10).add(Map.entry("x", 1));
    }

    @Test
    void reportFormat() {
        assertEquals("Total words: 3\nDistinct words: 2\n  b: 2\n  a: 1\n", WordStats.report("b a b", 2));
    }
}
