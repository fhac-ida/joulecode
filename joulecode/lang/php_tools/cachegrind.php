<?php
/**
 * joule:code – analyses Xdebug profiles (Cachegrind format) in the benchmark container.
 *
 * Usage: php cachegrind.php <directory> '<json list of excluded paths>'
 * Output: one line "JOULECODE_JSON{...}" with self and total time per function
 * of the project files (relative to the working directory). Has no dependencies
 * because it runs in the user's image.
 */

$dir = $argv[1];
$excluded = json_decode($argv[2] ?? '[]', true) ?: [];
$root = rtrim(getcwd(), '/');

$stats = [];   // "file\0name" => [self, incl, calls]
$total = 0.0;

foreach (glob(rtrim($dir, '/') . '/*') ?: [] as $file) {
    $h = fopen($file, 'r');
    if (!$h) {
        continue;
    }
    $unit = 1e-7;        // Xdebug 2: 100 ns
    $timeIdx = 0;
    $files = [];
    $names = [];
    $fl = null;
    $fn = null;
    $key = null;
    $afterCalls = false;
    $callee = '';
    while (($line = fgets($h)) !== false) {
        $line = rtrim($line, "\r\n");
        if ($line === '') {
            continue;
        }
        if (str_starts_with($line, 'events:')) {
            $events = preg_split('/\s+/', trim(substr($line, 7)));
            foreach ($events as $i => $ev) {
                if (preg_match('/^Time(?:_\((\d+)(ns|us|ms)\))?$/', $ev, $m)) {
                    $timeIdx = $i;
                    if (isset($m[1])) {
                        $unit = (int) $m[1] * ['ns' => 1e-9, 'us' => 1e-6, 'ms' => 1e-3][$m[2]];
                    }
                }
            }
            continue;
        }
        if (preg_match('/^(c?)(fl|fi|fe|fn)=(?:\((\d+)\))?\s*(.*)$/', $line, $m)) {
            [$all, $isCall, $kind, $id, $name] = $m;
            if ($kind === 'fn') {
                $table = &$names;
            } else {
                $table = &$files;
            }
            if ($id !== '' && $name !== '') {
                $table[$id] = $name;
            }
            $value = $name !== '' ? $name : ($table[$id] ?? '');
            unset($table);
            if ($isCall) {
                if ($kind === 'fn') {
                    $callee = $value; // target of the following "calls="
                }
                continue;
            }
            if ($kind === 'fl') {
                $fl = $value;
            } elseif ($kind === 'fn') {
                $fn = $value;
                $key = $fl . "\0" . $fn;
                $stats[$key] ??= [0.0, 0.0, 0, 0];
                $stats[$key][2]++;
                $afterCalls = false;
            }
            continue;
        }
        if (str_starts_with($line, 'calls=')) {
            $afterCalls = true;
            continue;
        }
        if ($key !== null && preg_match('/^[+-]?\d+(\s+\d+)+/', $line)) {
            $parts = preg_split('/\s+/', $line);
            $lineNo = (int) $parts[0];
            $cost = ((float) ($parts[$timeIdx + 1] ?? 0)) * $unit;
            if ($afterCalls) {
                $stats[$key][1] += $cost;
                // Built-in functions (in_array, usort …) are work of the calling function –
                // like `x in some_list` in cProfile. Otherwise a loop with in_array, for example, would
                // not show up as a hotspot because its time ends up in php::in_array.
                if (str_starts_with($callee, 'php::')) {
                    $stats[$key][0] += $cost;
                }
                $afterCalls = false;
            } else {
                $stats[$key][0] += $cost;
                $stats[$key][1] += $cost;
                $total += $cost;
                if ($stats[$key][3] === 0) {
                    $stats[$key][3] = $lineNo;
                }
            }
        }
    }
    fclose($h);
}

$rows = [];
foreach ($stats as $key => [$self, $incl, $calls, $lineNo]) {
    [$path, $name] = explode("\0", $key, 2);
    if ($path === '' || !str_starts_with($path, $root . '/')) {
        continue;
    }
    $rel = substr($path, strlen($root) + 1);
    $skip = str_starts_with($rel, 'vendor/');
    foreach ($excluded as $e) {
        $e = rtrim($e, '/');
        if ($rel === $e || str_starts_with($rel, $e . '/')) {
            $skip = true;
        }
    }
    if ($skip) {
        continue;
    }
    $rows[] = ['file' => $rel, 'line' => $lineNo, 'function' => $name, 'ncalls' => $calls,
        'tottime' => $self, 'cumtime' => $incl];
}
echo 'JOULECODE_JSON' . json_encode(['total_s' => $total, 'functions' => $rows], JSON_UNESCAPED_SLASHES) . "\n";
