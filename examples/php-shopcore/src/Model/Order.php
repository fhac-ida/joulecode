<?php

declare(strict_types=1);

namespace ShopCore\Model;

final class Order
{
    /**
     * @param list<PricedLine> $lines
     */
    public function __construct(
        public readonly string $number,
        public readonly Customer $customer,
        public readonly array $lines,
        public readonly int $shippingCents,
        public readonly string $createdAt,
        public readonly ?string $coupon = null,
    ) {
    }

    public function itemsCents(): int
    {
        $sum = 0;
        foreach ($this->lines as $line) {
            $sum += $line->grossCents();
        }
        return $sum;
    }

    public function totalCents(): int
    {
        return $this->itemsCents() + $this->shippingCents;
    }

    public function taxCents(): int
    {
        $sum = 0;
        foreach ($this->lines as $line) {
            $sum += $line->taxCents();
        }
        return $sum;
    }

    /** @return array<string, int> tax per rate, keys e.g. "19.0" */
    public function taxBreakdown(): array
    {
        $out = [];
        foreach ($this->lines as $line) {
            $key = number_format($line->taxRate, 1, '.', '');
            $out[$key] = ($out[$key] ?? 0) + $line->taxCents();
        }
        ksort($out, SORT_STRING);
        return $out;
    }

    public function quantity(): int
    {
        return array_sum(array_map(static fn (PricedLine $l): int => $l->quantity, $this->lines));
    }
}
