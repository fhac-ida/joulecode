<?php

declare(strict_types=1);

namespace ShopCore\Model;

final class CartLine
{
    public function __construct(
        public readonly string $sku,
        public readonly int $quantity,
    ) {
        if ($quantity <= 0) {
            throw new \InvalidArgumentException("Quantity for {$sku} must be positive");
        }
    }
}
