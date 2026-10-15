<?php

declare(strict_types=1);

namespace ShopCore\Model;

/**
 * Item in the catalogue. Prices in cents (gross) so that no rounding errors occur.
 */
final class Product
{
    /**
     * @param list<string> $tags
     */
    public function __construct(
        public readonly string $sku,
        public readonly string $name,
        public readonly string $category,
        public readonly int $priceCents,
        public readonly TaxClass $taxClass,
        public readonly int $stock,
        public readonly array $tags = [],
        public readonly bool $active = true,
        public readonly int $weightGrams = 0,
    ) {
        if ($priceCents < 0) {
            throw new \InvalidArgumentException("Price of {$sku} is negative");
        }
    }

    public function hasTag(string $tag): bool
    {
        return in_array($tag, $this->tags, true);
    }

    public function isAvailable(): bool
    {
        return $this->active && $this->stock > 0;
    }

    public function withStock(int $stock): self
    {
        return new self(
            $this->sku,
            $this->name,
            $this->category,
            $this->priceCents,
            $this->taxClass,
            $stock,
            $this->tags,
            $this->active,
            $this->weightGrams,
        );
    }
}
