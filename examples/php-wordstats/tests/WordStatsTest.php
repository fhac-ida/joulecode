<?php

declare(strict_types=1);

namespace WordStats\Tests;

use PHPUnit\Framework\TestCase;
use WordStats\WordStats;

final class WordStatsTest extends TestCase
{
    private const TEXT = 'The dog and a cat and the bird and THE fish';

    public function testUniqueWordsKeepsFirstOccurrenceOrder(): void
    {
        self::assertSame(['the', 'dog', 'and', 'a', 'cat', 'bird', 'fish'], WordStats::uniqueWords(self::TEXT));
    }

    public function testUniqueWordsEmpty(): void
    {
        self::assertSame([], WordStats::uniqueWords(''));
    }

    public function testNumericWordsStayStrings(): void
    {
        self::assertSame(['1', '01', 'a'], WordStats::uniqueWords('1 01 a 1'));
    }

    public function testFrequentWordsCountsAndTieBreak(): void
    {
        self::assertSame([['and', 3], ['the', 3], ['a', 1]], WordStats::frequentWords(self::TEXT, 3));
    }

    public function testFrequentWordsNumericWordsStayStrings(): void
    {
        // array_count_values/array_flip turn '1' into the key int(1) – the word must stay a string.
        self::assertSame([['1', 2], ['a', 1]], WordStats::frequentWords('1 1 a', 5));
    }

    public function testFrequentWordsTopLargerThanVocabulary(): void
    {
        self::assertCount(2, WordStats::frequentWords('a b a', 10));
    }

    public function testReportFormat(): void
    {
        self::assertSame("Total words: 3\nDistinct words: 2\n  b: 2\n  a: 1\n", WordStats::report('b a b', 2));
    }
}
