<?php

declare(strict_types=1);

namespace ShopCore;

use ShopCore\Model\Order;

/**
 * Analyses and exports over orders: revenue per item, category and day,
 * best sellers, order CSV for accounting.
 *
 * Item numbers are strings and are compared as such (strcmp) –
 * “100” comes before “99”.
 */
final class Export
{
    /** @var list<Order> */
    private array $orders = [];

    /**
     * @param iterable<Order> $orders
     */
    public function __construct(private readonly Catalog $catalog, iterable $orders = [])
    {
        foreach ($orders as $order) {
            $this->orders[] = $order;
        }
    }

    public function add(Order $order): void
    {
        $this->orders[] = $order;
    }

    /** Revenue (gross, after discount) of an item over all orders. */
    public function revenueOf(string $sku): int
    {
        $sum = 0;
        foreach ($this->orders as $order) {
            foreach ($order->lines as $line) {
                if ($line->sku === $sku) {
                    $sum += $line->grossCents();
                }
            }
        }
        return $sum;
    }

    public function unitsOf(string $sku): int
    {
        $sum = 0;
        foreach ($this->orders as $order) {
            foreach ($order->lines as $line) {
                if ($line->sku === $sku) {
                    $sum += $line->quantity;
                }
            }
        }
        return $sum;
    }

    /** @return list<string> all sold item numbers in order of the first sale */
    public function soldSkus(): array
    {
        $seen = [];
        $out = [];
        foreach ($this->orders as $order) {
            foreach ($order->lines as $line) {
                if (!isset($seen[$line->sku])) {
                    $seen[$line->sku] = true;
                    $out[] = $line->sku;
                }
            }
        }
        return $out;
    }

    /**
     * Best sellers by revenue; with equal revenue by item number (strcmp).
     *
     * @return list<array{sku: string, name: string, revenue: int, units: int}>
     */
    public function topSellers(int $limit = 10): array
    {
        $skus = $this->soldSkus();
        usort($skus, function (string $a, string $b): int {
            $diff = $this->revenueOf($b) <=> $this->revenueOf($a);
            return $diff !== 0 ? $diff : strcmp($a, $b);
        });
        $out = [];
        foreach (array_slice($skus, 0, $limit) as $sku) {
            $out[] = [
                'sku' => $sku,
                'name' => $this->catalog->findBySku($sku)?->name ?? '?',
                'revenue' => $this->revenueOf($sku),
                'units' => $this->unitsOf($sku),
            ];
        }
        return $out;
    }

    /** @return array<string, int> category (top level) → revenue, sorted by category */
    public function revenueByCategory(): array
    {
        $out = [];
        foreach ($this->orders as $order) {
            foreach ($order->lines as $line) {
                $product = $this->catalog->findBySku($line->sku);
                $top = $product === null ? '?' : explode('/', $product->category)[0];
                $out[$top] = ($out[$top] ?? 0) + $line->grossCents();
            }
        }
        ksort($out, SORT_STRING);
        return $out;
    }

    /** @return array<string, array{orders: int, revenue: int}> day (YYYY-MM-DD) → key figures */
    public function daily(): array
    {
        $out = [];
        foreach ($this->orders as $order) {
            $day = substr($order->createdAt, 0, 10);
            $out[$day] ??= ['orders' => 0, 'revenue' => 0];
            $out[$day]['orders']++;
            $out[$day]['revenue'] += $order->totalCents();
        }
        ksort($out, SORT_STRING);
        return $out;
    }

    /** Average cart in cents (without shipping). */
    public function averageBasket(): float
    {
        if ($this->orders === []) {
            return 0.0;
        }
        $sum = 0;
        foreach ($this->orders as $order) {
            $sum += $order->itemsCents();
        }
        return $sum / count($this->orders);
    }

    /**
     * Orders as CSV for accounting: one line per tax rate and order.
     */
    public function accountingCsv(): string
    {
        $rows = ['order;date;customer;country;tax_rate;gross;tax'];
        foreach ($this->orders as $order) {
            $gross = [];
            foreach ($order->lines as $line) {
                $key = number_format($line->taxRate, 1, '.', '');
                $gross[$key] = ($gross[$key] ?? 0) + $line->grossCents();
            }
            ksort($gross, SORT_STRING);
            $tax = $order->taxBreakdown();
            foreach ($gross as $rate => $cents) {
                $rows[] = implode(';', [
                    $order->number,
                    substr($order->createdAt, 0, 10),
                    $order->customer->id,
                    $order->customer->country,
                    str_replace('.', ',', (string) $rate),
                    Money::format($cents, 'EUR'),
                    Money::format($tax[$rate] ?? 0, 'EUR'),
                ]);
            }
            if ($order->shippingCents > 0) {
                $rows[] = implode(';', [
                    $order->number,
                    substr($order->createdAt, 0, 10),
                    $order->customer->id,
                    $order->customer->country,
                    'shipping',
                    Money::format($order->shippingCents, 'EUR'),
                    Money::format(0, 'EUR'),
                ]);
            }
        }
        return implode("\n", $rows) . "\n";
    }

    /**
     * Text report for the management.
     */
    public function summary(string $title = 'Sales report'): string
    {
        $lines = [$title, str_repeat('=', mb_strlen($title)), ''];
        $total = 0;
        foreach ($this->orders as $order) {
            $total += $order->totalCents();
        }
        $lines[] = sprintf('Orders: %d  Revenue: %s  Avg cart: %s', count($this->orders), Money::format($total), Money::format((int) round($this->averageBasket())));
        $lines[] = '';
        $lines[] = 'Bestseller:';
        foreach ($this->topSellers(10) as $i => $row) {
            $lines[] = sprintf('%3d. %-10s %-30s %12s %5d pcs', $i + 1, $row['sku'], mb_substr($row['name'], 0, 30), Money::format($row['revenue']), $row['units']);
        }
        $lines[] = '';
        $lines[] = 'Revenue per category:';
        foreach ($this->revenueByCategory() as $category => $cents) {
            $lines[] = sprintf('  %-20s %14s', $category, Money::format($cents));
        }
        return implode("\n", $lines) . "\n";
    }
}
