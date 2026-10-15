<?php
/**
 * joule:code – helper tool for PHP source code based on nikic/PHP-Parser.
 *
 * Reads a JSON request from stdin, writes a JSON answer to stdout.
 * Operations:
 *   functions       {source}                 functions/methods with spans, signature, PHPDoc, complexity
 *   parse_function  {code, method}           checks that `code` is exactly one function/method
 *   lint            {source}                 syntax check
 *   file_info       {source}                 MI, SLOC, use statements, declared/referenced names
 *   analyze         {root, files}            functions + file_info for many files
 *
 * Runs in the image joulecode/php-tools; locally with JOULECODE_PHP_AUTOLOAD=<autoload.php>.
 */

declare(strict_types=1);

$autoload = getenv('JOULECODE_PHP_AUTOLOAD') ?: __DIR__ . '/vendor/autoload.php';
require $autoload;

use PhpParser\Error;
use PhpParser\Node;
use PhpParser\NodeFinder;
use PhpParser\NodeTraverser;
use PhpParser\NodeVisitor\NameResolver;
use PhpParser\ParserFactory;

function gc_parse(string $source): array
{
    $parser = (new ParserFactory())->createForNewestSupportedVersion();
    $stmts = $parser->parse($source) ?? [];
    $traverser = new NodeTraverser();
    $traverser->addVisitor(new NameResolver(null, ['preserveOriginalNames' => true, 'replaceNodes' => false]));
    $traverser->traverse($stmts);
    return $stmts;
}

// -- Complexity -----------------------------------------------------------------
/** Cyclomatic complexity (as PHPMD/pdepend CCN2): 1 + branches + boolean operators. */
function gc_complexity(Node $fn): int
{
    $cc = 1;
    $finder = new NodeFinder();
    foreach ($finder->find($fn->getStmts() ?? [], fn () => true) as $n) {
        if ($n instanceof Node\Stmt\If_ || $n instanceof Node\Stmt\ElseIf_
            || $n instanceof Node\Stmt\For_ || $n instanceof Node\Stmt\Foreach_
            || $n instanceof Node\Stmt\While_ || $n instanceof Node\Stmt\Do_
            || $n instanceof Node\Stmt\Catch_ || $n instanceof Node\Expr\Ternary
            || $n instanceof Node\Expr\BinaryOp\Coalesce || $n instanceof Node\Expr\AssignOp\Coalesce
            || $n instanceof Node\Expr\BinaryOp\BooleanAnd || $n instanceof Node\Expr\BinaryOp\BooleanOr
            || $n instanceof Node\Expr\BinaryOp\LogicalAnd || $n instanceof Node\Expr\BinaryOp\LogicalOr
            || $n instanceof Node\Expr\BinaryOp\LogicalXor) {
            $cc++;
        } elseif ($n instanceof Node\Stmt\Case_ && $n->cond !== null) {
            $cc++;
        } elseif ($n instanceof Node\MatchArm && $n->conds !== null) {
            $cc += count($n->conds);
        }
    }
    return $cc;
}

// -- Signature and PHPDoc ------------------------------------------------------------
function gc_type(?Node $t): string
{
    if ($t === null) {
        return '';
    }
    if ($t instanceof Node\Name) {
        return $t->toString();
    }
    if ($t instanceof Node\Identifier) {
        return strtolower($t->toString());
    }
    if ($t instanceof Node\NullableType) {
        return '?' . gc_type($t->type);
    }
    if ($t instanceof Node\UnionType) {
        $parts = array_map('gc_type', $t->types);
        sort($parts);
        return implode('|', $parts);
    }
    if ($t instanceof Node\IntersectionType) {
        $parts = array_map('gc_type', $t->types);
        sort($parts);
        return implode('&', $parts);
    }
    return get_class($t);
}

/** Comparable signature: name, parameters (name, type, default yes/no, by-ref, variadic), return, modifiers. */
function gc_signature(Node $fn): string
{
    $params = [];
    foreach ($fn->params as $p) {
        $params[] = sprintf(
            '%s%s$%s:%s%s',
            $p->byRef ? '&' : '',
            $p->variadic ? '...' : '',
            $p->var instanceof Node\Expr\Variable && is_string($p->var->name) ? $p->var->name : '?',
            gc_type($p->type),
            $p->default !== null ? '=' : ''
        );
    }
    $mods = '';
    if ($fn instanceof Node\Stmt\ClassMethod) {
        $mods = implode(',', array_filter([
            $fn->isPublic() ? 'public' : '', $fn->isProtected() ? 'protected' : '',
            $fn->isPrivate() ? 'private' : '', $fn->isStatic() ? 'static' : '',
            $fn->isAbstract() ? 'abstract' : '', $fn->isFinal() ? 'final' : '',
        ]));
    }
    return sprintf(
        '%s%s(%s):%s[%s]',
        $fn->byRef ? '&' : '',
        strtolower($fn->name->toString()),
        implode(',', $params),
        gc_type($fn->returnType),
        $mods
    );
}

/** Text of a PHPDoc block without comment characters. */
function gc_doc_text(string $doc): string
{
    $doc = preg_replace('#^/\*\*#', '', trim($doc));
    $doc = preg_replace('#\*/$#', '', $doc);
    $lines = [];
    foreach (preg_split('/\R/', $doc) as $line) {
        $lines[] = preg_replace('/^\s*\*( ?)/', '', $line);
    }
    return trim(implode("\n", $lines));
}

function gc_function_info(Node $fn, string $qualname): array
{
    $doc = $fn->getDocComment();
    $nodeStart = $fn->getStartLine();
    foreach ($fn->attrGroups as $g) {
        $nodeStart = min($nodeStart, $g->getStartLine());
    }
    $start = $doc ? min($nodeStart, $doc->getStartLine()) : $nodeStart;
    return [
        'qualname' => $qualname,
        'start' => $start,
        'end' => $fn->getEndLine(),
        'node_start' => $nodeStart,
        'def_line' => $fn->name->getStartLine(),
        'doc_start' => $doc ? $doc->getStartLine() : 0,
        'doc_end' => $doc ? $doc->getEndLine() : 0,
        'signature' => gc_signature($fn),
        'docstring' => $doc ? gc_doc_text($doc->getText()) : null,
        'complexity' => $fn->getStmts() === null ? 1 : gc_complexity($fn),
        'abstract' => $fn->getStmts() === null,
    ];
}

/** Functions and methods at top level or in named classes/traits/interfaces/enums. */
function gc_functions(array $stmts): array
{
    $out = [];
    $walk = function (array $stmts) use (&$walk, &$out): void {
        foreach ($stmts as $s) {
            if ($s instanceof Node\Stmt\Namespace_) {
                $walk($s->stmts);
            } elseif ($s instanceof Node\Stmt\Function_) {
                $out[] = gc_function_info($s, $s->name->toString());
            } elseif ($s instanceof Node\Stmt\ClassLike && $s->name !== null) {
                foreach ($s->getMethods() as $m) {
                    $out[] = gc_function_info($m, $s->name->toString() . '::' . $m->name->toString());
                }
            } elseif ($s instanceof Node\Stmt\Declare_ && $s->stmts) {
                $walk($s->stmts);
            }
        }
    };
    $walk($stmts);
    return $out;
}

// -- File metrics ---------------------------------------------------------------------
/** SLOC, comment lines and Halstead volume from the tokens. */
function gc_token_metrics(string $source): array
{
    $codeLines = [];
    $commentLines = [];
    $operators = [];
    $operands = [];
    $n1 = 0;
    $n2 = 0;
    $operandTokens = [T_VARIABLE, T_STRING, T_LNUMBER, T_DNUMBER, T_CONSTANT_ENCAPSED_STRING,
        T_ENCAPSED_AND_WHITESPACE, T_NAME_QUALIFIED, T_NAME_FULLY_QUALIFIED, T_NAME_RELATIVE, T_NUM_STRING];
    $ignore = [T_WHITESPACE, T_OPEN_TAG, T_CLOSE_TAG, T_INLINE_HTML];
    foreach (PhpToken::tokenize($source) as $tok) {
        if (in_array($tok->id, $ignore, true)) {
            continue;
        }
        $lines = substr_count($tok->text, "\n");
        if ($tok->id === T_COMMENT || $tok->id === T_DOC_COMMENT) {
            for ($l = $tok->line; $l <= $tok->line + $lines; $l++) {
                $commentLines[$l] = true;
            }
            continue;
        }
        for ($l = $tok->line; $l <= $tok->line + $lines; $l++) {
            $codeLines[$l] = true;
        }
        if (in_array($tok->id, $operandTokens, true)) {
            $operands[$tok->text] = true;
            $n2++;
        } else {
            $operators[strtolower($tok->text)] = true;
            $n1++;
        }
    }
    $vocabulary = count($operators) + count($operands);
    $length = $n1 + $n2;
    $volume = $vocabulary > 0 ? $length * log($vocabulary, 2) : 0.0;
    return [
        'sloc' => count($codeLines),
        'comments' => count(array_diff_key($commentLines, $codeLines)),
        'volume' => $volume,
    ];
}

/** Maintainability index as in radon (variant with comment share), 0–100. */
function gc_mi(float $volume, int $complexity, int $sloc, int $comments): float
{
    if ($volume <= 0 || $complexity <= 0 || $sloc <= 0) {
        return 100.0;
    }
    $commentPct = $comments * 100.0 / $sloc;
    $mi = 171 - 5.2 * log($volume) - 0.23 * $complexity - 16.2 * log($sloc)
        + 50 * sin(sqrt(2.46 * deg2rad($commentPct)));
    return round(min(max(0.0, $mi * 100 / 171), 100.0), 2);
}

function gc_is_builtin_class(string $name): bool
{
    return class_exists($name, false) || interface_exists($name, false)
        || trait_exists($name, false) || enum_exists($name, false);
}

/** use statements, insertion point for new ones, declared and referenced names. */
function gc_file_info(string $source, array $stmts, array $functions): array
{
    $finder = new NodeFinder();
    $uses = [];
    $useKinds = [];
    $insertAfter = 0;
    $namespace = null;
    // Insertion point: after the last `use` at top level or of the first namespace,
    // otherwise after `namespace …;`, otherwise after declare(strict_types=1), otherwise after `<?php`.
    $top = $stmts;
    foreach ($stmts as $s) {
        if ($s instanceof Node\Stmt\Declare_) {
            $insertAfter = max($insertAfter, $s->getEndLine());
        }
        if ($s instanceof Node\Stmt\Namespace_) {
            $namespace = $s->name ? $s->name->toString() : '';
            $insertAfter = max($insertAfter, $s->name ? $s->name->getEndLine() : $s->getStartLine());
            $top = $s->stmts;
            break;
        }
    }
    foreach ($top as $s) {
        if ($s instanceof Node\Stmt\Use_ || $s instanceof Node\Stmt\GroupUse) {
            $insertAfter = max($insertAfter, $s->getEndLine());
            $kind = [Node\Stmt\Use_::TYPE_FUNCTION => 'function ', Node\Stmt\Use_::TYPE_CONSTANT => 'const '][$s->type] ?? '';
            $prefix = $s instanceof Node\Stmt\GroupUse ? $s->prefix->toString() . '\\' : '';
            foreach ($s->uses as $u) {
                $k = $kind ?: ([Node\Stmt\Use_::TYPE_FUNCTION => 'function ', Node\Stmt\Use_::TYPE_CONSTANT => 'const '][$u->type] ?? '');
                $full = ltrim($prefix . $u->name->toString(), '\\');
                $alias = $u->alias ? ' as ' . $u->alias->toString() : '';
                $uses[] = 'use ' . $k . $full . $alias . ';';
                $useKinds[$full] = $k === '' ? 'class' : trim($k);
            }
        }
    }
    if ($insertAfter === 0) {
        $insertAfter = 1; // line with <?php
    }

    $declared = [];
    foreach ($finder->find($stmts, fn ($n) => $n instanceof Node\Stmt\ClassLike || $n instanceof Node\Stmt\Function_) as $n) {
        if (isset($n->namespacedName)) {
            $declared[] = $n->namespacedName->toString();
        }
    }

    $referenced = [];
    $add = function (?Node $name) use (&$referenced): void {
        if (!$name instanceof Node\Name) {
            return;
        }
        $s = $name->toString();
        if (in_array(strtolower($s), ['self', 'static', 'parent'], true)) {
            return;
        }
        $referenced[ltrim($s, '\\')] = 'class';
    };
    foreach ($finder->find($stmts, fn () => true) as $n) {
        if ($n instanceof Node\Expr\New_ || $n instanceof Node\Expr\StaticCall
            || $n instanceof Node\Expr\ClassConstFetch || $n instanceof Node\Expr\StaticPropertyFetch
            || $n instanceof Node\Expr\Instanceof_) {
            $add($n->class);
        } elseif ($n instanceof Node\Stmt\Catch_) {
            foreach ($n->types as $t) {
                $add($t);
            }
        } elseif ($n instanceof Node\Stmt\Class_) {
            $add($n->extends);
            foreach ($n->implements as $i) {
                $add($i);
            }
        } elseif ($n instanceof Node\Stmt\TraitUse) {
            foreach ($n->traits as $t) {
                $add($t);
            }
        } elseif ($n instanceof Node\Param || $n instanceof Node\FunctionLike) {
            $types = $n instanceof Node\Param ? [$n->type] : [$n->getReturnType()];
            while ($types) {
                $t = array_pop($types);
                if ($t instanceof Node\NullableType) {
                    $types[] = $t->type;
                } elseif ($t instanceof Node\UnionType || $t instanceof Node\IntersectionType) {
                    array_push($types, ...$t->types);
                } else {
                    $add($t);
                }
            }
        } elseif ($n instanceof Node\Expr\FuncCall && $n->name instanceof Node\Name) {
            // Unqualified calls fall back to the global function in PHP.
            $resolved = $n->name->getAttribute('namespacedName');
            $name = $n->name->isFullyQualified() || $n->name->isQualified()
                ? $n->name->toString() : ($resolved ? $resolved->toString() : $n->name->toString());
            if (!$n->name->isQualified() && !$n->name->isFullyQualified() && function_exists($n->name->toString())) {
                continue; // global built-in function
            }
            $referenced[ltrim($name, '\\')] = 'function';
        }
    }
    foreach ($useKinds as $name => $kind) {
        if ($kind !== 'const') {
            $referenced[$name] ??= $kind;
        }
    }
    $external = [];
    foreach ($referenced as $name => $kind) {
        $builtin = $kind === 'class' ? gc_is_builtin_class($name) : function_exists($name);
        if (!$builtin && !in_array(strtolower($name), ['mixed', 'iterable', 'callable', 'object'], true)) {
            $external[] = $name;
        }
    }
    sort($external);

    $tm = gc_token_metrics($source);
    $complexity = array_sum(array_map(fn ($f) => $f['complexity'], $functions));
    return [
        'namespace' => $namespace,
        'uses' => $uses,
        'use_insert_after' => $insertAfter,
        'declared' => $declared,
        'referenced' => $external,
        'sloc' => $tm['sloc'],
        'mi' => gc_mi($tm['volume'], max($complexity, 1), $tm['sloc'], $tm['comments']),
    ];
}

// -- Operations ----------------------------------------------------------------------
function gc_parse_function(string $code, bool $method): array
{
    $code = preg_replace('/^\s*<\?php\s*/', '', $code);
    $wrapped = $method ? "<?php\nclass __JouleCodeWrapper__ {\n" . $code . "\n}\n" : "<?php\n" . $code . "\n";
    $stmts = gc_parse($wrapped);
    if ($method) {
        $cls = $stmts[0] ?? null;
        if (count($stmts) !== 1 || !$cls instanceof Node\Stmt\Class_) {
            throw new RuntimeException('Expected exactly one method without further code.');
        }
        $members = $cls->stmts;
        if (count($members) !== 1 || !$members[0] instanceof Node\Stmt\ClassMethod) {
            throw new RuntimeException('Expected exactly one method without further class members.');
        }
        $fn = $members[0];
    } else {
        if (count($stmts) !== 1 || !$stmts[0] instanceof Node\Stmt\Function_) {
            throw new RuntimeException('Expected exactly one function definition without further code.');
        }
        $fn = $stmts[0];
    }
    return ['name' => $fn->name->toString(), 'code' => $code];
}

function gc_handle(array $req): array
{
    switch ($req['op']) {
        case 'functions':
            return ['functions' => gc_functions(gc_parse($req['source']))];
        case 'lint':
            gc_parse($req['source']);
            return [];
        case 'canonical':
            $tokens = [];
            foreach (PhpToken::tokenize($req['source']) as $tok) {
                if (!$tok->isIgnorable()) {
                    $tokens[] = $tok->text;
                }
            }
            return ['canonical' => implode("\0", $tokens)];
        case 'parse_function':
            return gc_parse_function($req['code'], (bool) $req['method']);
        case 'file_info':
            $stmts = gc_parse($req['source']);
            $functions = gc_functions($stmts);
            return gc_file_info($req['source'], $stmts, $functions);
        case 'analyze':
            $files = [];
            foreach ($req['files'] as $rel) {
                $source = file_get_contents(rtrim($req['root'], '/') . '/' . $rel);
                try {
                    $stmts = gc_parse($source);
                    $functions = gc_functions($stmts);
                    $files[$rel] = ['functions' => $functions, 'info' => gc_file_info($source, $stmts, $functions)];
                } catch (Error $e) {
                    $files[$rel] = ['error' => $e->getMessage()];
                }
            }
            return ['files' => $files];
    }
    throw new RuntimeException('Unknown operation: ' . $req['op']);
}

$req = json_decode(stream_get_contents(STDIN), true, 512, JSON_THROW_ON_ERROR);
try {
    $res = ['ok' => true] + gc_handle($req);
} catch (Error $e) {
    $res = ['ok' => false, 'error' => 'Syntax error: ' . $e->getMessage()];
} catch (Throwable $e) {
    $res = ['ok' => false, 'error' => $e->getMessage()];
}
echo json_encode($res, JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES | JSON_INVALID_UTF8_SUBSTITUTE);
