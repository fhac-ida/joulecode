<?php
/**
 * Rector configuration for joule:code: suggestions only (dry run), no changes.
 * codeQuality and deadCode provide findings for the optimiser – like Ruff for Python.
 */

declare(strict_types=1);

use Rector\Config\RectorConfig;

return RectorConfig::configure()
    ->withPreparedSets(deadCode: true, codeQuality: true)
    ->withCache('/tmp/joulecode-rector-cache');
