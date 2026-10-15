<?php

declare(strict_types=1);

namespace ShopCore;

/**
 * Format and parse amounts in cents (German notation, e.g. "1.234,56 €").
 */
final class Money
{
    public static function format(int $cents, string $currency = '€'): string
    {
        $sign = $cents < 0 ? '-' : '';
        $abs = abs($cents);
        return $sign . number_format(intdiv($abs, 100), 0, ',', '.') . ',' . str_pad((string) ($abs % 100), 2, '0', STR_PAD_LEFT) . ' ' . $currency;
    }

    public static function parse(string $text): int
    {
        $clean = trim(str_replace(['€', ' ', "\u{a0}"], '', $text));
        if (!preg_match('/^(-?)(\d{1,3}(?:\.\d{3})*|\d+)(?:,(\d{1,2}))?$/', $clean, $m)) {
            throw new \InvalidArgumentException("Not an amount: {$text}");
        }
        $euros = (int) str_replace('.', '', $m[2]);
        $cents = isset($m[3]) ? (int) str_pad($m[3], 2, '0') : 0;
        $value = $euros * 100 + $cents;
        return $m[1] === '-' ? -$value : $value;
    }

    /** Percentage share in cents, rounded half up. */
    public static function percent(int $cents, float $percent): int
    {
        return (int) round($cents * $percent / 100);
    }

    /**
     * Split an amount into parts without losing a cent (remainder on the first parts).
     *
     * @param list<int> $weights
     * @return list<int>
     */
    public static function allocate(int $cents, array $weights): array
    {
        $total = array_sum($weights);
        if ($total <= 0) {
            throw new \InvalidArgumentException('weights must be positive');
        }
        $parts = [];
        $assigned = 0;
        foreach ($weights as $w) {
            $part = intdiv($cents * $w, $total);
            $parts[] = $part;
            $assigned += $part;
        }
        for ($i = 0; $assigned < $cents; $i++, $assigned++) {
            $parts[$i % count($parts)]++;
        }
        return $parts;
    }
}
