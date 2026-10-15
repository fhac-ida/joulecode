<?php

declare(strict_types=1);

namespace ShopCore;

use ShopCore\Model\CartLine;
use ShopCore\Model\Customer;
use ShopCore\Model\PricedLine;
use ShopCore\Model\Product;

/**
 * Price rules: tiered prices, percentage discounts on categories or item lists,
 * “buy x, get y free”, discounts for customer groups.
 *
 * A rule is an array:
 *
 *     ['id' => 'R1', 'type' => 'percent', 'percent' => 10.0, 'skus' => [...], 'category' => '...',
 *      'groups' => ['b2b'], 'minQty' => 1, 'priority' => 10, 'exclusive' => false]
 *     ['type' => 'tier', 'skus' => [...], 'tiers' => [10 => 5.0, 50 => 8.0]]    // from quantity: per cent off the unit price
 *     ['type' => 'bogo', 'skus' => [...], 'buy' => 2, 'free' => 1]
 *
 * Order: higher `priority` first, on a tie in the order of creation.
 * An `exclusive` rule ends the processing of the line. Discounts are computed on the
 * respective remaining amount (not added up) and rounded to cents.
 */
final class Pricing
{
    /** @var list<array<string, mixed>> */
    private array $rules = [];

    /**
     * @param list<array<string, mixed>> $rules
     */
    public function __construct(private readonly Catalog $catalog, array $rules = [])
    {
        foreach ($rules as $rule) {
            $this->addRule($rule);
        }
    }

    /** @param array<string, mixed> $rule */
    public function addRule(array $rule): void
    {
        $rule += ['skus' => [], 'category' => null, 'groups' => [], 'minQty' => 1, 'priority' => 0, 'exclusive' => false];
        if (!isset($rule['id'], $rule['type'])) {
            throw new \InvalidArgumentException('rule needs id and type');
        }
        if (!in_array($rule['type'], ['percent', 'tier', 'bogo'], true)) {
            throw new \InvalidArgumentException("Unknown rule type {$rule['type']}");
        }
        $rule['seq'] = count($this->rules);
        $this->rules[] = $rule;
        usort($this->rules, static fn (array $a, array $b): int => [$b['priority'], $a['seq']] <=> [$a['priority'], $b['seq']]);
    }

    /** @return list<array<string, mixed>> */
    public function rules(): array
    {
        return $this->rules;
    }

    /**
     * Rules that apply to item, quantity and customer – in order of application.
     *
     * @return list<array<string, mixed>>
     */
    public function rulesFor(Product $product, int $quantity, ?Customer $customer): array
    {
        $out = [];
        foreach ($this->rules as $rule) {
            if ($quantity < $rule['minQty']) {
                continue;
            }
            if ($rule['groups'] !== [] && ($customer === null || !in_array($customer->group, $rule['groups'], true))) {
                continue;
            }
            $bySku = $rule['skus'] !== [] && in_array($product->sku, $rule['skus'], true);
            $byCategory = $rule['category'] !== null && Catalog::isInCategory($product->category, $rule['category']);
            $global = $rule['skus'] === [] && $rule['category'] === null;
            if ($bySku || $byCategory || $global) {
                $out[] = $rule;
            }
        }
        return $out;
    }

    public function priceLine(CartLine $line, ?Customer $customer, string $country): PricedLine
    {
        $product = $this->catalog->require($line->sku);
        $unit = $product->priceCents;
        $applied = [];
        $discount = 0;
        foreach ($this->rulesFor($product, $line->quantity, $customer) as $rule) {
            $remaining = $unit * $line->quantity - $discount;
            switch ($rule['type']) {
                case 'tier':
                    $percent = self::tierPercent($rule['tiers'], $line->quantity);
                    if ($percent > 0.0) {
                        $discount += (int) round($unit * $percent / 100) * $line->quantity;
                        $applied[] = $rule['id'];
                    }
                    break;
                case 'percent':
                    $discount += Money::percent($remaining, (float) $rule['percent']);
                    $applied[] = $rule['id'];
                    break;
                case 'bogo':
                    $bundles = intdiv($line->quantity, $rule['buy'] + $rule['free']);
                    if ($bundles > 0) {
                        $discount += min($remaining, $bundles * $rule['free'] * $unit);
                        $applied[] = $rule['id'];
                    }
                    break;
            }
            if ($rule['exclusive'] && in_array($rule['id'], $applied, true)) {
                break;
            }
        }
        $discount = min($discount, $unit * $line->quantity);
        $rate = $customer !== null && $customer->vatExempt ? 0.0 : $product->taxClass->rate($country);
        return new PricedLine($product->sku, $product->name, $line->quantity, $unit, $discount, $rate, $applied);
    }

    /**
     * Discount of the highest tier reached.
     *
     * @param array<int, float> $tiers minimum quantity → per cent
     */
    public static function tierPercent(array $tiers, int $quantity): float
    {
        $best = 0.0;
        $bestQty = 0;
        foreach ($tiers as $minQty => $percent) {
            if ($quantity >= $minQty && $minQty >= $bestQty) {
                $best = (float) $percent;
                $bestQty = $minQty;
            }
        }
        return $best;
    }

    /**
     * @param list<CartLine> $lines
     * @return list<PricedLine>
     */
    public function priceCart(array $lines, ?Customer $customer, string $country): array
    {
        return array_map(fn (CartLine $l): PricedLine => $this->priceLine($l, $customer, $country), self::mergeLines($lines));
    }

    /**
     * Merge equal items; the order of first occurrence is kept.
     *
     * @param list<CartLine> $lines
     * @return list<CartLine>
     */
    public static function mergeLines(array $lines): array
    {
        $qty = [];
        $order = [];
        foreach ($lines as $line) {
            if (!array_key_exists($line->sku, $qty)) {
                $order[] = $line->sku;
                $qty[$line->sku] = 0;
            }
            $qty[$line->sku] += $line->quantity;
        }
        return array_map(static fn (string $sku): CartLine => new CartLine($sku, $qty[$sku]), $order);
    }

    // -- Coupons ---------------------------------------------------------------------

    /**
     * Coupon on the cart total: per cent or fixed amount, from a minimum order value.
     * The discount is distributed proportionally (by line amount) across the lines so that the
     * tax per rate is correct.
     *
     * @param list<PricedLine> $lines
     * @param array{code: string, percent?: float, cents?: int, minCents?: int, excludeReduced?: bool} $coupon
     * @return list<PricedLine>
     */
    public static function applyCoupon(array $lines, array $coupon): array
    {
        $eligible = [];
        foreach ($lines as $i => $line) {
            if (($coupon['excludeReduced'] ?? false) && $line->taxRate < 15.0 && $line->taxRate > 0.0) {
                continue;
            }
            $eligible[$i] = $line->grossCents();
        }
        $base = array_sum($eligible);
        if ($base === 0 || $base < ($coupon['minCents'] ?? 0)) {
            return $lines;
        }
        $total = isset($coupon['percent']) ? Money::percent($base, $coupon['percent']) : min($base, $coupon['cents'] ?? 0);
        $parts = Money::allocate($total, array_values($eligible));
        $out = $lines;
        foreach (array_keys($eligible) as $k => $i) {
            $l = $lines[$i];
            $out[$i] = new PricedLine(
                $l->sku,
                $l->name,
                $l->quantity,
                $l->unitCents,
                $l->discountCents + $parts[$k],
                $l->taxRate,
                [...$l->appliedRules, 'coupon:' . $coupon['code']],
            );
        }
        return $out;
    }

    /**
     * Savings compared with the list price per rule (for controlling).
     *
     * @param list<PricedLine> $lines
     * @return array<string, int>
     */
    public static function savingsByRule(array $lines): array
    {
        $out = [];
        foreach ($lines as $line) {
            $n = count($line->appliedRules);
            if ($n === 0) {
                continue;
            }
            $parts = Money::allocate($line->discountCents, array_fill(0, $n, 1));
            foreach ($line->appliedRules as $k => $id) {
                $out[$id] = ($out[$id] ?? 0) + $parts[$k];
            }
        }
        ksort($out, SORT_STRING);
        return $out;
    }
}
