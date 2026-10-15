<?php

declare(strict_types=1);

namespace ShopCore\Model;

/**
 * Priced cart line. All amounts in cents; `discountCents` is the sum of all
 * discounts of this line, `appliedRules` the IDs of the applied price rules.
 */
final class PricedLine
{
    /**
     * @param list<string> $appliedRules
     */
    public function __construct(
        public readonly string $sku,
        public readonly string $name,
        public readonly int $quantity,
        public readonly int $unitCents,
        public readonly int $discountCents,
        public readonly float $taxRate,
        public readonly array $appliedRules = [],
    ) {
    }

    public function grossCents(): int
    {
        return $this->unitCents * $this->quantity - $this->discountCents;
    }

    public function taxCents(): int
    {
        // Prices are gross: extract the included tax, rounded half up.
        return (int) round($this->grossCents() * $this->taxRate / (100 + $this->taxRate));
    }

    public function netCents(): int
    {
        return $this->grossCents() - $this->taxCents();
    }
}
