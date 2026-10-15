<?php

declare(strict_types=1);

namespace WordStats;

/**
 * Text statistics (original variant with typical inefficiencies).
 */
final class WordStats
{
    /**
     * All words in order of their first occurrence, lower-cased.
     *
     * @return list<string>
     */
    public static function uniqueWords(string $text): array
    {
        $seen = [];
        foreach (self::words($text) as $word) {
            if (!in_array($word, $seen, true)) { // search in a list: O(n)
                $seen[] = $word;
            }
        }
        return $seen;
    }

    /**
     * The `$top` most frequent words with their count, ties sorted alphabetically.
     *
     * @return list<array{0: string, 1: int}>
     */
    public static function frequentWords(string $text, int $top = 10): array
    {
        $words = self::words($text);
        $counts = [];
        foreach (self::uniqueWords($text) as $word) {
            $n = 0;
            foreach ($words as $other) { // nested loop: O(n*m)
                if ($other === $word) {
                    $n++;
                }
            }
            $counts[] = [$word, $n];
        }
        usort($counts, fn (array $a, array $b): int => [$b[1], $a[0]] <=> [$a[1], $b[0]]);
        return array_slice($counts, 0, $top);
    }

    /**
     * Short text report.
     */
    public static function report(string $text, int $top = 5): string
    {
        $out = '';
        $out .= 'Total words: ' . count(self::words($text)) . "\n";
        $out .= 'Distinct words: ' . count(self::uniqueWords($text)) . "\n";
        foreach (self::frequentWords($text, $top) as [$word, $n]) {
            $out .= "  {$word}: {$n}\n"; // string concatenation in the loop
        }
        return $out;
    }

    /**
     * Words of the text, lower-cased, split at whitespace.
     *
     * @return list<string>
     */
    private static function words(string $text): array
    {
        $parts = preg_split('/\s+/', mb_strtolower($text), -1, PREG_SPLIT_NO_EMPTY);
        return $parts === false ? [] : $parts;
    }
}
