<?php

declare(strict_types=1);

namespace ShopCore\Model;

enum TaxClass: string
{
    case STANDARD = 'standard';
    case REDUCED = 'reduced';
    case EXEMPT = 'exempt';

    /** Tax rate in per cent per country of delivery. */
    public function rate(string $country): float
    {
        $rates = [
            'DE' => ['standard' => 19.0, 'reduced' => 7.0],
            'AT' => ['standard' => 20.0, 'reduced' => 10.0],
            'NL' => ['standard' => 21.0, 'reduced' => 9.0],
            'BE' => ['standard' => 21.0, 'reduced' => 6.0],
            'FR' => ['standard' => 20.0, 'reduced' => 5.5],
        ];
        if ($this === self::EXEMPT) {
            return 0.0;
        }
        if (!isset($rates[$country])) {
            throw new \InvalidArgumentException("No tax rates for country {$country}");
        }
        return $rates[$country][$this->value];
    }
}
