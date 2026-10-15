<?php

/**
 * Benchmark: fixed, deterministic amount of work (one sales day of a web shop).
 *
 * BENCH_ROUNDS scales the run time (on the measurement machine, choose it so that a run takes
 * >= 20 s). BENCH_PRODUCTS / BENCH_ORDERS change the amount of data per round.
 */

declare(strict_types=1);

require __DIR__ . '/../vendor/autoload.php';

use ShopCore\Catalog;
use ShopCore\Checkout;
use ShopCore\CheckoutException;
use ShopCore\Export;
use ShopCore\Inventory;
use ShopCore\Model\CartLine;
use ShopCore\Model\Customer;
use ShopCore\Model\Product;
use ShopCore\Model\TaxClass;
use ShopCore\Pricing;

$rounds = (int) (getenv('BENCH_ROUNDS') ?: 3);
$productCount = (int) (getenv('BENCH_PRODUCTS') ?: 3000);
$orderCount = (int) (getenv('BENCH_ORDERS') ?: 400);

function build(int $productCount, int $orderCount): array
{
    mt_srand(1234);
    $categories = ['garten/werkzeug', 'garten/pflanzen', 'haushalt/kueche', 'haushalt/bad', 'buero/papier', 'buero/technik', 'freizeit/sport', 'freizeit/camping'];
    $tagPool = ['neu', 'sale', 'bio', 'eco', 'premium', 'set', 'xl', 'kinder', '2024', '2025', 'outdoor', 'akku', 'holz', 'metall', 'glas'];
    $products = [];
    for ($i = 0; $i < $productCount; $i++) {
        $tags = [];
        foreach (array_rand(array_flip($tagPool), mt_rand(2, 5)) as $t) {
            $tags[] = (string) $t;
        }
        $products[] = new Product(
            sprintf('%06d', 100000 + $i * 7),
            'Item ' . $i . ' ' . $tagPool[$i % count($tagPool)],
            $categories[$i % count($categories)],
            mt_rand(199, 49999),
            $i % 9 === 0 ? TaxClass::REDUCED : TaxClass::STANDARD,
            mt_rand(0, 400),
            $tags,
            $i % 41 !== 0,
            mt_rand(50, 30000),
        );
    }
    $catalog = new Catalog($products);
    $skus = array_map(static fn (Product $p): string => $p->sku, $products);
    $rules = [];
    for ($r = 0; $r < 160; $r++) {
        $pick = array_rand($skus, 250);
        $rule = ['id' => sprintf('R%03d', $r), 'priority' => $r % 5, 'skus' => array_map(static fn (int $k): string => $skus[$k], $pick)];
        $rules[] = match ($r % 4) {
            0 => $rule + ['type' => 'percent', 'percent' => 5.0 + $r % 20],
            1 => $rule + ['type' => 'tier', 'tiers' => [3 => 3.0, 10 => 8.0]],
            2 => $rule + ['type' => 'bogo', 'buy' => 2, 'free' => 1],
            3 => ['id' => sprintf('C%03d', $r), 'type' => 'percent', 'percent' => 3.0, 'category' => $categories[$r % count($categories)], 'groups' => ['b2b'], 'priority' => 1],
        };
    }
    $carts = [];
    for ($o = 0; $o < $orderCount; $o++) {
        $lines = [];
        for ($k = mt_rand(2, 8); $k > 0; $k--) {
            $lines[] = new CartLine($skus[mt_rand(0, count($skus) - 1)], mt_rand(1, 12));
        }
        $carts[] = [
            new Customer("K{$o}", "Customer {$o}", ['b2c', 'b2b', 'b2c'][$o % 3], ['DE', 'DE', 'AT', 'NL', 'FR'][$o % 5]),
            $lines,
            sprintf('2026-05-%02dT%02d:%02d:00', 1 + $o % 28, 8 + $o % 12, $o % 60),
        ];
    }
    return [$products, $rules, $carts];
}

[$products, $rules, $carts] = build($productCount, $orderCount);

$start = hrtime(true);
$checksum = 0;
for ($round = 0; $round < $rounds; $round++) {
    $catalog = new Catalog($products);
    $pricing = new Pricing($catalog, $rules);
    $inventory = new Inventory();
    $inventory->addWarehouse('Aachen', 1);
    $inventory->addWarehouse('Köln', 2);
    foreach ($products as $p) {
        $inventory->setStock($p->sku, 'Aachen', intdiv($p->stock, 2) + 1000);
        $inventory->setStock($p->sku, 'Köln', $p->stock - intdiv($p->stock, 2));
    }
    $checkout = new Checkout($catalog, $pricing, $inventory);
    $checkout->addCoupon(['code' => 'SOMMER10', 'percent' => 10.0, 'minCents' => 5000, 'excludeReduced' => true]);
    $export = new Export($catalog);
    foreach ($carts as $i => [$customer, $lines, $createdAt]) {
        try {
            $order = $checkout->placeOrder($lines, $customer, $createdAt, $i % 7 === 0 ? 'sommer10' : null);
            $export->add($order);
            $checksum += $order->totalCents() % 1000003;
        } catch (CheckoutException $e) {
            $checksum += strlen($e->getMessage());
        }
    }
    foreach (['werkzeug', 'bio set', 'premium', '', 'item 12', 'sale xl'] as $text) {
        foreach (['', 'garten', 'buero', 'freizeit/sport'] as $category) {
            $res = $catalog->search(['text' => $text, 'category' => $category, 'available' => true, 'sort' => $text === '' ? 'price' : 'relevance']);
            $checksum += $res['total'] + count($res['facets']);
            $checksum += array_sum(array_map(static fn (array $f): int => $f[1], $res['facets']));
        }
    }
    $checksum += count($catalog->related($products[17]->sku)) + count($catalog->categoryTree());
    $checksum += strlen($export->summary()) + strlen($export->accountingCsv());
    $checksum += count($catalog->validate());
}
printf("rounds=%d checksum=%d seconds=%.2f\n", $rounds, $checksum, (hrtime(true) - $start) / 1e9);
