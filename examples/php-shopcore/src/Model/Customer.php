<?php

declare(strict_types=1);

namespace ShopCore\Model;

final class Customer
{
    public function __construct(
        public readonly string $id,
        public readonly string $name,
        public readonly string $group,
        public readonly string $country,
        public readonly bool $vatExempt = false,
    ) {
        if (!in_array($group, ['b2c', 'b2b', 'staff'], true)) {
            throw new \InvalidArgumentException("Unknown customer group {$group}");
        }
    }

    public function isBusiness(): bool
    {
        return $this->group === 'b2b';
    }
}
