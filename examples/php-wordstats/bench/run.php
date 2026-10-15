<?php

/**
 * Benchmark: fixed, deterministic amount of work.
 *
 * The energy for exactly this work is measured. BENCH_ROUNDS scales the
 * run time (on the measurement machine, choose it so that a run takes >= 20 s).
 */

declare(strict_types=1);

require __DIR__ . '/../vendor/autoload.php';

use WordStats\WordStats;

$rounds = (int) (getenv('BENCH_ROUNDS') ?: 40);
$vocab = (int) (getenv('BENCH_VOCAB') ?: 1500);
$wordsPerText = (int) (getenv('BENCH_WORDS') ?: 6000);

mt_srand(1234);
$vocabulary = [];
for ($i = 0; $i < $vocab; $i++) {
    $vocabulary[] = "word{$i}";
}
$texts = [];
for ($t = 0; $t < 5; $t++) {
    $words = [];
    for ($i = 0; $i < $wordsPerText; $i++) {
        $words[] = $vocabulary[mt_rand(0, $vocab - 1)];
    }
    $texts[] = implode(' ', $words);
}

$start = hrtime(true);
$checksum = 0;
for ($i = 0; $i < $rounds; $i++) {
    $text = $texts[$i % count($texts)];
    $checksum += count(WordStats::uniqueWords($text));
    $checksum += array_sum(array_column(WordStats::frequentWords($text, 20), 1));
    $checksum += strlen(WordStats::report($text, 10));
}
printf("rounds=%d checksum=%d seconds=%.2f\n", $rounds, $checksum, (hrtime(true) - $start) / 1e9);
