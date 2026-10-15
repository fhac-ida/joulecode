<?php

declare(strict_types=1);

namespace ShopCore\Tests;

use PHPUnit\Framework\TestCase;
use ShopCore\Catalog;
use ShopCore\Checkout;
use ShopCore\CheckoutException;
use ShopCore\Export;
use ShopCore\Inventory;
use ShopCore\Model\CartLine;
use ShopCore\Model\Customer;
use ShopCore\Model\Product;
use ShopCore\Model\TaxClass;
use ShopCore\Money;
use ShopCore\Pricing;

final class ShopCoreTest extends TestCase
{
    private function catalog(): Catalog
    {
        return new Catalog([
            new Product('A-1', 'Gartenschere Profi', 'garten/werkzeug', 2499, TaxClass::STANDARD, 10, ['metall', 'premium'], true, 300),
            new Product('A-2', 'Rosenerde 20 l', 'garten/pflanzen', 899, TaxClass::REDUCED, 50, ['bio'], true, 21000),
            new Product('B-1', 'Kaffeemühle', 'haushalt/kueche', 4999, TaxClass::STANDARD, 3, ['metall', 'neu'], true, 900),
            new Product('B-2', 'Alte Mühle', 'haushalt/kueche', 1999, TaxClass::STANDARD, 0, ['holz'], false),
        ]);
    }

    private function inventory(Catalog $catalog): Inventory
    {
        $inv = new Inventory();
        $inv->addWarehouse('Aachen', 1);
        foreach ($catalog->all() as $p) {
            $inv->setStock($p->sku, 'Aachen', $p->stock);
        }
        return $inv;
    }

    public function testMoneyFormatAndParse(): void
    {
        self::assertSame('1.234,56 €', Money::format(123456));
        self::assertSame('-0,05 €', Money::format(-5));
        self::assertSame(123456, Money::parse('1.234,56 €'));
        self::assertSame([34, 33, 33], Money::allocate(100, [1, 1, 1]));
    }

    public function testFindBySkuAndRequire(): void
    {
        $catalog = $this->catalog();
        self::assertSame('Kaffeemühle', $catalog->findBySku('B-1')?->name);
        self::assertNull($catalog->findBySku('X'));
        $this->expectException(\OutOfBoundsException::class);
        $catalog->require('X');
    }

    public function testCategoryTreeCountsActiveProducts(): void
    {
        self::assertSame(
            ['garten' => 2, 'garten/pflanzen' => 1, 'garten/werkzeug' => 1, 'haushalt' => 1, 'haushalt/kueche' => 1],
            $this->catalog()->categoryTree(),
        );
    }

    public function testSearchByTextAndCategory(): void
    {
        $res = $this->catalog()->search(['text' => 'mühle']);
        self::assertSame(['B-1'], array_map(static fn (Product $p): string => $p->sku, $res['items']));
        $res = $this->catalog()->search(['category' => 'garten', 'sort' => 'price']);
        self::assertSame(['A-2', 'A-1'], array_map(static fn (Product $p): string => $p->sku, $res['items']));
    }

    public function testFacetCounts(): void
    {
        $res = $this->catalog()->search([]);
        self::assertSame([['metall', 2], ['bio', 1], ['neu', 1], ['premium', 1]], $res['facets']);
    }

    public function testPercentRuleAndTierRule(): void
    {
        $catalog = $this->catalog();
        $pricing = new Pricing($catalog, [
            ['id' => 'P10', 'type' => 'percent', 'percent' => 10.0, 'skus' => ['A-1']],
            ['id' => 'T', 'type' => 'tier', 'skus' => ['A-1'], 'tiers' => [5 => 4.0], 'priority' => 5],
        ]);
        $line = $pricing->priceLine(new CartLine('A-1', 5), null, 'DE');
        // tier first (higher priority): 100 cents per piece, then 10% of the rest.
        self::assertSame(['T', 'P10'], $line->appliedRules);
        self::assertSame(500 + 1200, $line->discountCents);
        self::assertSame(19.0, $line->taxRate);
    }

    public function testBuyTwoGetOne(): void
    {
        $pricing = new Pricing($this->catalog(), [['id' => 'BOGO', 'type' => 'bogo', 'skus' => ['A-2'], 'buy' => 2, 'free' => 1]]);
        self::assertSame(2 * 899, $pricing->priceLine(new CartLine('A-2', 7), null, 'DE')->discountCents);
    }

    public function testMergeLinesKeepsFirstOrder(): void
    {
        $merged = Pricing::mergeLines([new CartLine('B-1', 1), new CartLine('A-1', 2), new CartLine('B-1', 3)]);
        self::assertSame([['B-1', 4], ['A-1', 2]], array_map(static fn (CartLine $l): array => [$l->sku, $l->quantity], $merged));
    }

    public function testCheckoutPlacesOrderWithShippingAndCoupon(): void
    {
        $catalog = $this->catalog();
        $checkout = new Checkout($catalog, new Pricing($catalog), $this->inventory($catalog));
        $checkout->addCoupon(['code' => 'WILLKOMMEN', 'cents' => 500]);
        $customer = new Customer('K1', 'Erika', 'b2c', 'DE');
        $order = $checkout->placeOrder([new CartLine('A-1', 1), new CartLine('A-2', 1)], $customer, '2026-05-04T10:00:00', 'willkommen');
        self::assertSame('GC-20260504-00001', $order->number);
        self::assertSame(2499 + 899 - 500, $order->itemsCents());
        self::assertSame(495 + 1990, $order->shippingCents); // below 50 €, bulky goods (soil, 21 kg)
    }

    public function testCheckoutRejectsUnavailable(): void
    {
        $catalog = $this->catalog();
        $checkout = new Checkout($catalog, new Pricing($catalog), $this->inventory($catalog));
        $this->expectException(CheckoutException::class);
        $checkout->placeOrder([new CartLine('B-1', 4)], new Customer('K1', 'Erika', 'b2c', 'DE'), '2026-05-04T10:00:00');
    }

    public function testInventoryReservesByPriority(): void
    {
        $inv = new Inventory();
        $inv->addWarehouse('Köln', 2);
        $inv->addWarehouse('Aachen', 1);
        $inv->setStock('A-1', 'Aachen', 3);
        $inv->setStock('A-1', 'Köln', 5);
        self::assertSame(['Aachen' => 3, 'Köln' => 2], $inv->reserve('A-1', 5));
        self::assertSame(3, $inv->available('A-1'));
    }

    public function testExportTopSellersAndCsv(): void
    {
        $catalog = $this->catalog();
        $checkout = new Checkout($catalog, new Pricing($catalog), $this->inventory($catalog));
        $customer = new Customer('K1', 'Erika', 'b2c', 'AT');
        $export = new Export($catalog, [
            $checkout->placeOrder([new CartLine('A-1', 2)], $customer, '2026-05-04T10:00:00'),
            $checkout->placeOrder([new CartLine('A-2', 1), new CartLine('B-1', 1)], $customer, '2026-05-05T10:00:00'),
        ]);
        self::assertSame(['B-1', 'A-1', 'A-2'], array_column($export->topSellers(), 'sku'));
        $csv = explode("\n", trim($export->accountingCsv()));
        self::assertSame('GC-20260504-00001;2026-05-04;K1;AT;20,0;49,98 EUR;8,33 EUR', $csv[1]);
    }
}
