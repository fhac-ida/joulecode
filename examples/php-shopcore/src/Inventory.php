<?php

declare(strict_types=1);

namespace ShopCore;

/**
 * Stock levels across several warehouses with reservations.
 *
 * Available means stock minus reservations. Reservations are taken from the warehouses in the
 * order of their priority (lower number first); `release` releases in reverse
 * order.
 */
final class Inventory
{
    /** @var array<string, int> warehouse → priority */
    private array $warehouses = [];

    /** @var array<string, array<string, int>> item → warehouse → stock */
    private array $stock = [];

    /** @var array<string, array<string, int>> item → warehouse → reserved */
    private array $reserved = [];

    public function addWarehouse(string $name, int $priority): void
    {
        $this->warehouses[$name] = $priority;
        asort($this->warehouses);
    }

    public function setStock(string $sku, string $warehouse, int $quantity): void
    {
        if (!isset($this->warehouses[$warehouse])) {
            throw new \OutOfBoundsException("Unknown warehouse {$warehouse}");
        }
        $this->stock[$sku][$warehouse] = $quantity;
    }

    public function onHand(string $sku): int
    {
        return array_sum($this->stock[$sku] ?? []);
    }

    public function available(string $sku): int
    {
        return $this->onHand($sku) - array_sum($this->reserved[$sku] ?? []);
    }

    /**
     * @return array<string, int> warehouse → reserved quantity of this reservation
     */
    public function reserve(string $sku, int $quantity): array
    {
        if ($quantity > $this->available($sku)) {
            throw new CheckoutException("{$sku}: not enough stock");
        }
        $taken = [];
        foreach (array_keys($this->warehouses) as $warehouse) {
            $free = ($this->stock[$sku][$warehouse] ?? 0) - ($this->reserved[$sku][$warehouse] ?? 0);
            if ($free <= 0) {
                continue;
            }
            $take = min($free, $quantity);
            $this->reserved[$sku][$warehouse] = ($this->reserved[$sku][$warehouse] ?? 0) + $take;
            $taken[$warehouse] = $take;
            $quantity -= $take;
            if ($quantity === 0) {
                break;
            }
        }
        return $taken;
    }

    public function release(string $sku, int $quantity): void
    {
        foreach (array_reverse(array_keys($this->warehouses)) as $warehouse) {
            $held = $this->reserved[$sku][$warehouse] ?? 0;
            $give = min($held, $quantity);
            if ($give > 0) {
                $this->reserved[$sku][$warehouse] = $held - $give;
                $quantity -= $give;
            }
            if ($quantity === 0) {
                return;
            }
        }
        if ($quantity > 0) {
            throw new \LogicException("{$sku}: more released than reserved");
        }
    }

    /** The reservation becomes a delivery: stock and reservation decrease. */
    public function ship(string $sku, int $quantity): void
    {
        foreach (array_keys($this->warehouses) as $warehouse) {
            $held = $this->reserved[$sku][$warehouse] ?? 0;
            $out = min($held, $quantity);
            if ($out > 0) {
                $this->reserved[$sku][$warehouse] -= $out;
                $this->stock[$sku][$warehouse] -= $out;
                $quantity -= $out;
            }
        }
        if ($quantity > 0) {
            throw new \LogicException("{$sku}: delivery without a reservation");
        }
    }

    /** @return array<string, int> warehouse → stock of an item */
    public function byWarehouse(string $sku): array
    {
        $out = [];
        foreach (array_keys($this->warehouses) as $warehouse) {
            $out[$warehouse] = $this->stock[$sku][$warehouse] ?? 0;
        }
        return $out;
    }

    /** @return list<string> items whose stock falls below the reorder level */
    public function reorderList(array $reorderPoints): array
    {
        $out = [];
        foreach ($reorderPoints as $sku => $point) {
            if ($this->available((string) $sku) <= $point) {
                $out[] = (string) $sku;
            }
        }
        sort($out, SORT_STRING);
        return $out;
    }
}
