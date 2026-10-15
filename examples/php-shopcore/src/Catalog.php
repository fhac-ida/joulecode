<?php

declare(strict_types=1);

namespace ShopCore;

use ShopCore\Model\Product;

/**
 * Product catalogue: category tree, search with filters and facets, sorting,
 * paging, related items and plausibility checks of the master data.
 *
 * Categories are paths with “/” as the separator, e.g. “garten/werkzeug/scheren” (garden/tools/shears).
 * A filter on “garten” finds all items below it.
 */
final class Catalog
{
    /** @var list<Product> */
    private array $products = [];

    /** @var array<string, string> category path → display name */
    private array $categoryNames = [];

    /**
     * @param iterable<Product> $products
     * @param array<string, string> $categoryNames
     */
    public function __construct(iterable $products = [], array $categoryNames = [])
    {
        foreach ($products as $product) {
            $this->add($product);
        }
        $this->categoryNames = $categoryNames;
    }

    public function add(Product $product): void
    {
        $this->products[] = $product;
    }

    public function count(): int
    {
        return count($this->products);
    }

    /** @return list<Product> */
    public function all(): array
    {
        return $this->products;
    }

    /**
     * Item for the item number. With a duplicate number the item created first applies.
     */
    public function findBySku(string $sku): ?Product
    {
        foreach ($this->products as $product) {
            if ($product->sku === $sku) {
                return $product;
            }
        }
        return null;
    }

    public function require(string $sku): Product
    {
        $product = $this->findBySku($sku);
        if ($product === null) {
            throw new \OutOfBoundsException("Unknown item number {$sku}");
        }
        return $product;
    }

    /**
     * New stock level for an item (replaces the item in its place).
     */
    public function updateStock(string $sku, int $stock): void
    {
        foreach ($this->products as $i => $product) {
            if ($product->sku === $sku) {
                $this->products[$i] = $product->withStock($stock);
                return;
            }
        }
        throw new \OutOfBoundsException("Unknown item number {$sku}");
    }

    // -- Categories ------------------------------------------------------------------

    public static function isInCategory(string $productCategory, string $filter): bool
    {
        if ($filter === '') {
            return true;
        }
        return $productCategory === $filter || str_starts_with($productCategory, $filter . '/');
    }

    /** @return list<string> all levels of a path, e.g. [garten, garten/werkzeug] */
    public static function ancestors(string $category): array
    {
        $parts = explode('/', $category);
        $out = [];
        for ($i = 1; $i <= count($parts); $i++) {
            $out[] = implode('/', array_slice($parts, 0, $i));
        }
        return $out;
    }

    /**
     * Category tree with the number of items per node (including subcategories).
     *
     * @return array<string, int> sorted by path
     */
    public function categoryTree(bool $onlyActive = true): array
    {
        $tree = [];
        foreach ($this->products as $product) {
            if ($onlyActive && !$product->active) {
                continue;
            }
            foreach (self::ancestors($product->category) as $node) {
                $tree[$node] = ($tree[$node] ?? 0) + 1;
            }
        }
        ksort($tree, SORT_STRING);
        return $tree;
    }

    public function categoryName(string $path): string
    {
        if (isset($this->categoryNames[$path])) {
            return $this->categoryNames[$path];
        }
        $last = substr($path, (int) strrpos('/' . $path, '/'));
        return ucfirst(str_replace('-', ' ', $last));
    }

    /** @return list<string> breadcrumb navigation as display names */
    public function breadcrumbs(string $category): array
    {
        return array_map(fn (string $p): string => $this->categoryName($p), self::ancestors($category));
    }

    // -- Search ------------------------------------------------------------------------

    /**
     * @param array{
     *     text?: string,
     *     category?: string,
     *     minCents?: int,
     *     maxCents?: int,
     *     tags?: list<string>,
     *     available?: bool,
     *     sort?: string,
     *     page?: int,
     *     perPage?: int
     * } $query
     * @return array{items: list<Product>, total: int, pages: int, facets: list<array{0: string, 1: int}>}
     */
    public function search(array $query): array
    {
        $text = mb_strtolower(trim($query['text'] ?? ''));
        $words = $text === '' ? [] : preg_split('/\s+/', $text);
        $hits = [];
        foreach ($this->products as $product) {
            if (!$product->active) {
                continue;
            }
            if (!self::isInCategory($product->category, $query['category'] ?? '')) {
                continue;
            }
            if (isset($query['minCents']) && $product->priceCents < $query['minCents']) {
                continue;
            }
            if (isset($query['maxCents']) && $product->priceCents > $query['maxCents']) {
                continue;
            }
            if (($query['available'] ?? false) && !$product->isAvailable()) {
                continue;
            }
            $ok = true;
            foreach ($query['tags'] ?? [] as $tag) {
                if (!$product->hasTag($tag)) {
                    $ok = false;
                    break;
                }
            }
            if (!$ok) {
                continue;
            }
            $score = $this->score($product, $words);
            if ($words !== [] && $score === 0) {
                continue;
            }
            $hits[] = [$product, $score];
        }

        $hits = $this->sortHits($hits, $query['sort'] ?? ($words === [] ? 'name' : 'relevance'));
        $items = array_map(static fn (array $h): Product => $h[0], $hits);
        $perPage = max(1, $query['perPage'] ?? 24);
        $page = max(1, $query['page'] ?? 1);
        return [
            'items' => array_slice($items, ($page - 1) * $perPage, $perPage),
            'total' => count($items),
            'pages' => (int) ceil(count($items) / $perPage),
            'facets' => self::facetCounts($items),
        ];
    }

    /**
     * Relevance: hits in the name count three times, in tags twice, in the item number once.
     *
     * @param list<string> $words
     */
    private function score(Product $product, array $words): int
    {
        if ($words === []) {
            return 0;
        }
        $name = mb_strtolower($product->name);
        $score = 0;
        foreach ($words as $word) {
            if (str_contains($name, $word)) {
                $score += 3;
            }
            foreach ($product->tags as $tag) {
                if (mb_strtolower($tag) === $word) {
                    $score += 2;
                }
            }
            if (str_contains(mb_strtolower($product->sku), $word)) {
                $score += 1;
            }
        }
        return $score;
    }

    /**
     * @param list<array{0: Product, 1: int}> $hits
     * @return list<array{0: Product, 1: int}>
     */
    private function sortHits(array $hits, string $sort): array
    {
        $cmp = match ($sort) {
            'price' => static fn (array $a, array $b): int => [$a[0]->priceCents, $a[0]->sku] <=> [$b[0]->priceCents, $b[0]->sku],
            'price_desc' => static fn (array $a, array $b): int => [$b[0]->priceCents, $a[0]->sku] <=> [$a[0]->priceCents, $b[0]->sku],
            'name' => static fn (array $a, array $b): int => [self::sortName($a[0]->name), $a[0]->sku] <=> [self::sortName($b[0]->name), $b[0]->sku],
            'relevance' => static fn (array $a, array $b): int => [$b[1], $a[0]->sku] <=> [$a[1], $b[0]->sku],
            default => throw new \InvalidArgumentException("Unknown sort order {$sort}"),
        };
        usort($hits, $cmp);
        return $hits;
    }

    /** Name for sorting: lower case, German umlauts as in the phone book (ä = ae). */
    public static function sortName(string $name): string
    {
        return strtr(mb_strtolower($name), ['ä' => 'ae', 'ö' => 'oe', 'ü' => 'ue', 'ß' => 'ss']);
    }

    /**
     * How often does every tag occur in the items? Most frequent first, on a tie by tag.
     *
     * Result as a list of pairs so that tags like “2024” stay strings.
     *
     * @param list<Product> $products
     * @return list<array{0: string, 1: int}>
     */
    public static function facetCounts(array $products): array
    {
        $all = [];
        foreach ($products as $product) {
            $all = array_merge($all, $product->tags);
        }
        $pairs = [];
        foreach (array_unique($all) as $tag) {
            $n = 0;
            foreach ($all as $other) {
                if ($other === $tag) {
                    $n++;
                }
            }
            $pairs[] = [(string) $tag, $n];
        }
        usort($pairs, static fn (array $a, array $b): int => [$b[1], $a[0]] <=> [$a[1], $b[0]]);
        return $pairs;
    }

    // -- Recommendations -------------------------------------------------------------

    /**
     * Related items: same category or shared tags, by number of shared tags.
     *
     * @return list<Product>
     */
    public function related(string $sku, int $limit = 6): array
    {
        $base = $this->require($sku);
        $scored = [];
        foreach ($this->products as $product) {
            if ($product->sku === $base->sku || !$product->isAvailable()) {
                continue;
            }
            $common = count(array_intersect($base->tags, $product->tags));
            $sameCategory = $product->category === $base->category ? 1 : 0;
            if ($common === 0 && $sameCategory === 0) {
                continue;
            }
            $scored[] = [$product, $common * 2 + $sameCategory];
        }
        usort($scored, static fn (array $a, array $b): int => [$b[1], $a[0]->sku] <=> [$a[1], $b[0]->sku]);
        return array_map(static fn (array $s): Product => $s[0], array_slice($scored, 0, $limit));
    }

    /**
     * “Customers also bought”: items that occur in the same orders.
     *
     * @param list<list<string>> $baskets item numbers per order
     * @return list<string>
     */
    public function boughtTogether(string $sku, array $baskets, int $limit = 5): array
    {
        $counts = [];
        foreach ($baskets as $basket) {
            if (!in_array($sku, $basket, true)) {
                continue;
            }
            foreach (array_unique($basket) as $other) {
                if ($other !== $sku) {
                    $counts[$other] = ($counts[$other] ?? 0) + 1;
                }
            }
        }
        $pairs = [];
        foreach ($counts as $other => $n) {
            $pairs[] = [(string) $other, $n];
        }
        usort($pairs, static fn (array $a, array $b): int => [$b[1], $a[0]] <=> [$a[1], $b[0]]);
        return array_map(static fn (array $p): string => $p[0], array_slice($pairs, 0, $limit));
    }

    // -- Checks -------------------------------------------------------------------------

    /** @return list<string> problems in the master data */
    public function validate(): array
    {
        $problems = [];
        $seen = [];
        foreach ($this->products as $product) {
            if (isset($seen[$product->sku])) {
                $problems[] = "Duplicate item number {$product->sku}";
            }
            $seen[$product->sku] = true;
            if (trim($product->name) === '') {
                $problems[] = "{$product->sku}: name missing";
            }
            if ($product->active && $product->priceCents === 0) {
                $problems[] = "{$product->sku}: active, but without a price";
            }
            if (!preg_match('#^[a-z0-9-]+(/[a-z0-9-]+)*$#', $product->category)) {
                $problems[] = "{$product->sku}: invalid category “{$product->category}”";
            }
            if (count($product->tags) !== count(array_unique($product->tags))) {
                $problems[] = "{$product->sku}: duplicate tags";
            }
        }
        return $problems;
    }

    /** @return array{min: int, max: int, avg: float, median: float} price statistics in cents */
    public function priceStats(string $category = ''): array
    {
        $prices = [];
        foreach ($this->products as $product) {
            if ($product->active && self::isInCategory($product->category, $category)) {
                $prices[] = $product->priceCents;
            }
        }
        if ($prices === []) {
            return ['min' => 0, 'max' => 0, 'avg' => 0.0, 'median' => 0.0];
        }
        sort($prices);
        $n = count($prices);
        $median = $n % 2 === 1 ? (float) $prices[intdiv($n, 2)] : ($prices[$n / 2 - 1] + $prices[$n / 2]) / 2;
        return ['min' => $prices[0], 'max' => $prices[$n - 1], 'avg' => array_sum($prices) / $n, 'median' => $median];
    }

    /**
     * Items with low stock, by stock, then item number.
     *
     * @return list<Product>
     */
    public function lowStock(int $threshold = 5): array
    {
        $low = array_values(array_filter(
            $this->products,
            static fn (Product $p): bool => $p->active && $p->stock <= $threshold,
        ));
        usort($low, static fn (Product $a, Product $b): int => [$a->stock, $a->sku] <=> [$b->stock, $b->sku]);
        return $low;
    }

    /**
     * Catalogue as CSV (semicolon, prices in euros with a decimal comma).
     */
    public function toCsv(): string
    {
        $rows = ['sku;name;category;price;stock;tags'];
        foreach ($this->products as $p) {
            $rows[] = implode(';', [
                $p->sku,
                str_replace(';', ',', $p->name),
                $p->category,
                number_format($p->priceCents / 100, 2, ',', ''),
                (string) $p->stock,
                implode('|', $p->tags),
            ]);
        }
        return implode("\n", $rows) . "\n";
    }
}
