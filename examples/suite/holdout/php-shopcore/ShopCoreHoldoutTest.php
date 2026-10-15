<?php

declare(strict_types=1);

namespace ShopCore\Holdout;

use PHPUnit\Framework\TestCase;
use ShopCore\Catalog;
use ShopCore\Checkout;
use ShopCore\Export;
use ShopCore\Inventory;
use ShopCore\Model\CartLine;
use ShopCore\Model\Customer;
use ShopCore\Model\Product;
use ShopCore\Model\TaxClass;
use ShopCore\Pricing;

/**
 * Holdout tests for php-shopcore – joule:code does not see them.
 */
final class ShopCoreHoldoutTest extends TestCase
{
    private static function product(string $sku, int $cents = 1000, array $tags = [], int $stock = 10, string $name = ''): Product
    {
        return new Product($sku, $name ?: "Item {$sku}", 'test/cat', $cents, TaxClass::STANDARD, $stock, $tags);
    }

    public function testDuplicateSkuFirstWins(): void
    {
        $catalog = new Catalog([self::product('A', 100, [], 1, 'first'), self::product('A', 200, [], 1, 'second')]);
        self::assertSame('first', $catalog->findBySku('A')?->name);
    }

    public function testAddedLaterIsFound(): void
    {
        $catalog = new Catalog([self::product('A')]);
        self::assertNull($catalog->findBySku('B'));
        $catalog->add(self::product('B', 500));
        self::assertSame(500, $catalog->findBySku('B')?->priceCents);
    }

    public function testUpdateStockIsVisible(): void
    {
        $catalog = new Catalog([self::product('A', 100, [], 3)]);
        $catalog->findBySku('A');
        $catalog->updateStock('A', 42);
        self::assertSame(42, $catalog->findBySku('A')?->stock);
        self::assertSame(42, $catalog->require('A')->stock);
    }

    public function testNumericLookingSkusStayDistinct(): void
    {
        $catalog = new Catalog([self::product('0100', 1), self::product('100', 2), self::product('1e2', 3)]);
        self::assertSame(1, $catalog->findBySku('0100')?->priceCents);
        self::assertSame(2, $catalog->findBySku('100')?->priceCents);
        self::assertSame(3, $catalog->findBySku('1e2')?->priceCents);
        self::assertNull($catalog->findBySku('00100'));
    }

    public function testFacetTagsStayStrings(): void
    {
        $facets = Catalog::facetCounts([
            self::product('A', 1, ['2024', 'neu']),
            self::product('B', 1, ['2024', '7']),
            self::product('C', 1, ['neu', '10']),
        ]);
        // tie: comparison with <=> – two numeric words numerically (“7” before “10”), otherwise as text.
        self::assertSame([['2024', 2], ['neu', 2], ['7', 1], ['10', 1]], $facets);
    }

    public function testFacetCountsEmpty(): void
    {
        self::assertSame([], Catalog::facetCounts([]));
        self::assertSame([], Catalog::facetCounts([self::product('A')]));
    }

    public function testRuleOrderPriorityThenCreation(): void
    {
        $catalog = new Catalog([self::product('100', 10000)]);
        $pricing = new Pricing($catalog, [
            ['id' => 'first', 'type' => 'percent', 'percent' => 10.0, 'skus' => ['100']],
            ['id' => 'global', 'type' => 'percent', 'percent' => 50.0, 'priority' => 0],
            ['id' => 'high', 'type' => 'percent', 'percent' => 20.0, 'skus' => ['0100', '100'], 'priority' => 9],
            ['id' => 'wrong', 'type' => 'percent', 'percent' => 90.0, 'skus' => ['0100', '1e2']],
        ]);
        $line = $pricing->priceLine(new CartLine('100', 1), null, 'DE');
        self::assertSame(['high', 'first', 'global'], $line->appliedRules);
        // 20% of 100.00 = 20.00; 10% of 80.00 = 8.00; 50% of 72.00 = 36.00
        self::assertSame(6400, $line->discountCents);
    }

    public function testExclusiveRuleStops(): void
    {
        $catalog = new Catalog([self::product('A', 1000)]);
        $pricing = new Pricing($catalog, [
            ['id' => 'X', 'type' => 'percent', 'percent' => 10.0, 'skus' => ['A'], 'exclusive' => true, 'priority' => 2],
            ['id' => 'Y', 'type' => 'percent', 'percent' => 10.0, 'skus' => ['A']],
        ]);
        self::assertSame(['X'], $pricing->priceLine(new CartLine('A', 1), null, 'DE')->appliedRules);
    }

    public function testGroupRestrictedRule(): void
    {
        $catalog = new Catalog([self::product('A', 1000)]);
        $pricing = new Pricing($catalog, [['id' => 'B2B', 'type' => 'percent', 'percent' => 10.0, 'groups' => ['b2b']]]);
        self::assertSame([], $pricing->priceLine(new CartLine('A', 1), null, 'DE')->appliedRules);
        $b2b = new Customer('K', 'Company', 'b2b', 'DE');
        self::assertSame(['B2B'], $pricing->priceLine(new CartLine('A', 1), $b2b, 'DE')->appliedRules);
    }

    public function testTopSellersTieBreakIsStringOrder(): void
    {
        $catalog = new Catalog([self::product('99', 1000), self::product('100', 1000), self::product('A', 500)]);
        $inv = new Inventory();
        $inv->addWarehouse('L', 1);
        foreach (['99', '100', 'A'] as $sku) {
            $inv->setStock($sku, 'L', 100);
        }
        $checkout = new Checkout($catalog, new Pricing($catalog), $inv);
        $c = new Customer('K', 'K', 'b2c', 'DE');
        $export = new Export($catalog, [
            $checkout->placeOrder([new CartLine('99', 1), new CartLine('A', 2)], $c, '2026-05-01T10:00:00'),
            $checkout->placeOrder([new CartLine('100', 1)], $c, '2026-05-01T11:00:00'),
        ]);
        $top = $export->topSellers();
        self::assertSame(['100', '99', 'A'], array_column($top, 'sku'));
        self::assertSame([1000, 1000, 1000], array_column($top, 'revenue'));
        self::assertSame([1, 1, 2], array_column($top, 'units'));
        self::assertSame('100', $top[0]['sku']);
    }
}
