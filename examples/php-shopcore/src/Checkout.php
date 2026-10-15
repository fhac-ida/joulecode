<?php

declare(strict_types=1);

namespace ShopCore;

use ShopCore\Model\CartLine;
use ShopCore\Model\Customer;
use ShopCore\Model\Order;
use ShopCore\Model\PricedLine;

/**
 * From cart to order: availability, prices, coupon, shipping, order number.
 */
final class Checkout
{
    /** Shipping costs in cents per country: [free from, standard, bulky goods surcharge] */
    private const SHIPPING = [
        'DE' => [5000, 495, 1990],
        'AT' => [7500, 990, 2990],
        'NL' => [7500, 990, 2990],
        'BE' => [7500, 990, 2990],
        'FR' => [10000, 1290, 3490],
    ];

    private const BULKY_GRAMS = 20000;

    private int $sequence = 0;

    /** @var array<string, array<string, mixed>> */
    private array $coupons = [];

    public function __construct(
        private readonly Catalog $catalog,
        private readonly Pricing $pricing,
        private readonly Inventory $inventory,
        private readonly string $prefix = 'GC',
    ) {
    }

    /** @param array{code: string, percent?: float, cents?: int, minCents?: int, excludeReduced?: bool, groups?: list<string>} $coupon */
    public function addCoupon(array $coupon): void
    {
        $this->coupons[strtoupper($coupon['code'])] = $coupon;
    }

    /**
     * Checks the cart without changing anything.
     *
     * @param list<CartLine> $lines
     * @return list<string> problems (empty = OK)
     */
    public function validate(array $lines, Customer $customer): array
    {
        $problems = [];
        if (!isset(self::SHIPPING[$customer->country])) {
            $problems[] = "No shipping to {$customer->country}";
        }
        foreach (Pricing::mergeLines($lines) as $line) {
            $product = $this->catalog->findBySku($line->sku);
            if ($product === null) {
                $problems[] = "Unknown item {$line->sku}";
                continue;
            }
            if (!$product->active) {
                $problems[] = "{$line->sku} is no longer available";
            } elseif ($this->inventory->available($line->sku) < $line->quantity) {
                $problems[] = "{$line->sku}: only {$this->inventory->available($line->sku)} available";
            }
        }
        return $problems;
    }

    /**
     * @param list<CartLine> $lines
     */
    public function placeOrder(array $lines, Customer $customer, string $createdAt, ?string $couponCode = null): Order
    {
        $problems = $this->validate($lines, $customer);
        if ($problems !== []) {
            throw new CheckoutException(implode('; ', $problems));
        }
        $priced = $this->pricing->priceCart($lines, $customer, $customer->country);
        $coupon = null;
        if ($couponCode !== null) {
            $coupon = $this->coupons[strtoupper($couponCode)] ?? null;
            if ($coupon === null) {
                throw new CheckoutException("Unknown coupon {$couponCode}");
            }
            if (isset($coupon['groups']) && !in_array($customer->group, $coupon['groups'], true)) {
                throw new CheckoutException("Coupon {$couponCode} is not valid for this customer group");
            }
            $priced = Pricing::applyCoupon($priced, $coupon);
        }
        foreach ($priced as $line) {
            $this->inventory->reserve($line->sku, $line->quantity);
        }
        $this->sequence++;
        $number = sprintf('%s-%s-%05d', $this->prefix, substr(str_replace('-', '', $createdAt), 0, 8), $this->sequence);
        return new Order($number, $customer, $priced, $this->shipping($priced, $customer->country), $createdAt, $coupon['code'] ?? null);
    }

    /**
     * @param list<PricedLine> $lines
     */
    public function shipping(array $lines, string $country): int
    {
        [$freeFrom, $standard, $bulky] = self::SHIPPING[$country] ?? throw new CheckoutException("No shipping to {$country}");
        $items = 0;
        $grams = 0;
        foreach ($lines as $line) {
            $items += $line->grossCents();
            $grams += $this->catalog->require($line->sku)->weightGrams * $line->quantity;
        }
        $cost = $items >= $freeFrom ? 0 : $standard;
        if ($grams >= self::BULKY_GRAMS) {
            $cost += $bulky;
        }
        return $cost;
    }

    /**
     * Cart preview for display (without reservation).
     *
     * @param list<CartLine> $lines
     * @return array{lines: list<PricedLine>, items: int, shipping: int, total: int, tax: array<string, int>}
     */
    public function preview(array $lines, Customer $customer): array
    {
        $priced = $this->pricing->priceCart($lines, $customer, $customer->country);
        $order = new Order('PREVIEW', $customer, $priced, $this->shipping($priced, $customer->country), '');
        return [
            'lines' => $priced,
            'items' => $order->itemsCents(),
            'shipping' => $order->shippingCents,
            'total' => $order->totalCents(),
            'tax' => $order->taxBreakdown(),
        ];
    }
}
