package wordstats;

import java.util.AbstractMap;
import java.util.ArrayList;
import java.util.List;
import java.util.Locale;
import java.util.Map;

/**
 * Text statistics (original variant with typical inefficiencies).
 */
public final class WordStats {

    private WordStats() {
    }

    /**
     * All words in order of their first occurrence, lower-cased.
     *
     * @param text input
     * @return words without repetition
     */
    public static List<String> uniqueWords(String text) {
        List<String> seen = new ArrayList<>();
        for (String word : words(text)) {
            if (!seen.contains(word)) { // search in a list: O(n)
                seen.add(word);
            }
        }
        return seen;
    }

    /**
     * The {@code top} most frequent words with their count, ties sorted alphabetically.
     *
     * @param text input
     * @param top  maximum number of entries
     * @return pairs (word, count), descending by count
     */
    public static List<Map.Entry<String, Integer>> frequentWords(String text, int top) {
        List<String> all = words(text);
        List<Map.Entry<String, Integer>> counts = new ArrayList<>();
        for (String word : uniqueWords(text)) {
            int n = 0;
            for (String other : all) { // nested loop: O(n*m)
                if (other.equals(word)) {
                    n++;
                }
            }
            counts.add(new AbstractMap.SimpleEntry<>(word, n));
        }
        counts.sort((a, b) -> a.getValue().equals(b.getValue())
                ? a.getKey().compareTo(b.getKey())
                : b.getValue() - a.getValue());
        return new ArrayList<>(counts.subList(0, Math.min(top, counts.size())));
    }

    /**
     * Short text report.
     *
     * @param text input
     * @param top  number of most frequent words in the report
     * @return report, one item per line
     */
    public static String report(String text, int top) {
        String out = "";
        out += "Total words: " + words(text).size() + "\n";
        out += "Distinct words: " + uniqueWords(text).size() + "\n";
        for (Map.Entry<String, Integer> e : frequentWords(text, top)) {
            out += "  " + e.getKey() + ": " + e.getValue() + "\n"; // string concatenation in the loop
        }
        return out;
    }

    /**
     * Words of the text, lower-cased, split at whitespace.
     */
    private static List<String> words(String text) {
        List<String> out = new ArrayList<>();
        for (String w : text.toLowerCase(Locale.ROOT).split("\\s+")) {
            if (!w.isEmpty()) {
                out.add(w);
            }
        }
        return out;
    }
}
