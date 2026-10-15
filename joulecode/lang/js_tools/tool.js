#!/usr/bin/env node
/**
 * joule:code – helper tool for JavaScript/TypeScript based on the TypeScript compiler API.
 *
 * Reads a JSON request from stdin, writes a JSON answer to stdout
 * (same schema as tool.php, see joulecode/lang/tooling.py):
 *   functions       {source}            functions/methods with spans, signature, JSDoc, complexity
 *   parse_function  {code, method}      checks that `code` is exactly one function/method
 *   lint            {source}            syntax check
 *   file_info       {source[, path]}    MI, SLOC, imports, modules, declared/referenced files
 *   analyze         {root, files}       functions + file_info for many files
 *
 * In the image joulecode/js-tools, `typescript` lies next to it; locally: JOULECODE_JS_NODE_MODULES=<node_modules>.
 */
'use strict';

const fs = require('fs');
const path = require('path');
const { createRequire, builtinModules } = require('module');

const nodeModules = process.env.JOULECODE_JS_NODE_MODULES;
const req = nodeModules ? createRequire(path.join(nodeModules, 'noop.js')) : require;
const ts = req('typescript');

const BUILTINS = new Set(builtinModules.flatMap((m) => [m, `node:${m}`]));
const SOURCE_EXT = ['.ts', '.tsx', '.mts', '.cts', '.js', '.jsx', '.mjs', '.cjs'];

function scriptKind(file) {
  const ext = path.extname(file || '').toLowerCase();
  if (ext === '.tsx' || ext === '.jsx') return ts.ScriptKind.TSX;
  // TypeScript also understands plain JavaScript; JSX only in .jsx/.tsx.
  return ts.ScriptKind.TS;
}

function parse(source, file) {
  const sf = ts.createSourceFile(file || 'input.ts', source, ts.ScriptTarget.Latest, true, scriptKind(file));
  const diags = sf.parseDiagnostics || [];
  if (diags.length) {
    const d = diags[0];
    const { line } = sf.getLineAndCharacterOfPosition(d.start || 0);
    const msg = ts.flattenDiagnosticMessageText(d.messageText, '\n');
    throw new SyntaxError(`Syntax error: ${msg} in line ${line + 1}`);
  }
  return sf;
}

const lineOf = (sf, pos) => sf.getLineAndCharacterOfPosition(pos).line + 1;

// -- Complexity ---------------------------------------------------------------------
const BRANCH = new Set([
  ts.SyntaxKind.IfStatement, ts.SyntaxKind.ConditionalExpression, ts.SyntaxKind.ForStatement,
  ts.SyntaxKind.ForInStatement, ts.SyntaxKind.ForOfStatement, ts.SyntaxKind.WhileStatement,
  ts.SyntaxKind.DoStatement, ts.SyntaxKind.CatchClause, ts.SyntaxKind.CaseClause,
]);
const LOGICAL = new Set([
  ts.SyntaxKind.AmpersandAmpersandToken, ts.SyntaxKind.BarBarToken, ts.SyntaxKind.QuestionQuestionToken,
  ts.SyntaxKind.AmpersandAmpersandEqualsToken, ts.SyntaxKind.BarBarEqualsToken,
  ts.SyntaxKind.QuestionQuestionEqualsToken,
]);

/** Cyclomatic complexity as ESLint `complexity`: 1 + branches + logical operators. */
function complexity(node) {
  let cc = 1;
  const visit = (n) => {
    if (BRANCH.has(n.kind)) cc++;
    else if (n.kind === ts.SyntaxKind.BinaryExpression && LOGICAL.has(n.operatorToken.kind)) cc++;
    ts.forEachChild(n, visit);
  };
  if (node) ts.forEachChild(node, visit);
  return cc;
}

// -- Functions ---------------------------------------------------------------------
const isFnExpr = (n) => n && (ts.isArrowFunction(n) || ts.isFunctionExpression(n));

function docOf(node) {
  const docs = node.jsDoc;
  return docs && docs.length ? docs[docs.length - 1] : null;
}

function docText(raw) {
  return raw
    .replace(/^\/\*\*/, '')
    .replace(/\*\/$/, '')
    .split(/\r?\n/)
    .map((l) => l.replace(/^\s*\*( ?)/, ''))
    .join('\n')
    .trim();
}

function modifiers(node) {
  const mods = (ts.canHaveModifiers(node) && ts.getModifiers(node)) || [];
  return mods.map((m) => ts.tokenToString(m.kind)).sort();
}

function normalize(text) {
  return text.replace(/\s+/g, ' ').trim();
}

/** Comparable signature: name, parameters (name, ?, ..., default, type), return type, modifiers. */
function signature(sf, name, fn, outer) {
  const params = fn.parameters.map((p) =>
    [
      modifiers(p).map((m) => `${m} `).join(''), // Parameter-Properties (private readonly x)
      p.dotDotDotToken ? '...' : '',
      normalize(p.name.getText(sf)),
      p.questionToken ? '?' : '',
      p.type ? `:${normalize(p.type.getText(sf))}` : '',
      p.initializer ? '=' : '',
    ].join(''),
  );
  const ret = fn.type ? normalize(fn.type.getText(sf)) : '';
  const mods = [...new Set([...modifiers(outer), ...modifiers(fn)])].join(',');
  const star = fn.asteriskToken ? '*' : '';
  return `${star}${name}(${params.join(',')}):${ret}[${mods}]`;
}

function info(sf, qualname, outer, fn, nameNode) {
  const doc = docOf(outer);
  const nodeStart = lineOf(sf, outer.getStart(sf));
  const docStart = doc ? lineOf(sf, doc.pos) : 0;
  return {
    qualname,
    start: doc ? Math.min(docStart, nodeStart) : nodeStart,
    end: lineOf(sf, outer.end),
    node_start: nodeStart,
    def_line: lineOf(sf, nameNode.getStart(sf)),
    doc_start: docStart,
    doc_end: doc ? lineOf(sf, doc.end) : 0,
    signature: signature(sf, nameNode.getText(sf), fn, outer),
    docstring: doc ? docText(sf.text.slice(doc.pos, doc.end)) || null : null,
    complexity: complexity(fn.body),
    abstract: !fn.body,
  };
}

/** Function from a variable statement `const f = (…) => …` or `= function (…) {…}`. */
function varFunction(stmt) {
  if (!ts.isVariableStatement(stmt)) return null;
  const decls = stmt.declarationList.declarations;
  if (decls.length !== 1 || !ts.isIdentifier(decls[0].name) || !isFnExpr(decls[0].initializer)) return null;
  return { name: decls[0].name, fn: decls[0].initializer };
}

/** Class member that is a method (incl. `foo = () => {}`). */
function memberFunction(m) {
  if ((ts.isMethodDeclaration(m) || ts.isConstructorDeclaration(m)) && (m.name || ts.isConstructorDeclaration(m))) {
    return { name: m.name, fn: m, label: ts.isConstructorDeclaration(m) ? 'constructor' : null };
  }
  if (ts.isPropertyDeclaration(m) && isFnExpr(m.initializer)) return { name: m.name, fn: m.initializer };
  return null;
}

function functions(sf) {
  const out = [];
  for (const stmt of sf.statements) {
    if (ts.isFunctionDeclaration(stmt) && stmt.name) {
      out.push(info(sf, stmt.name.text, stmt, stmt, stmt.name));
    } else if (ts.isClassDeclaration(stmt) && stmt.name) {
      for (const m of stmt.members) {
        const mf = memberFunction(m);
        if (!mf) continue;
        const nameNode = mf.name || m.getFirstToken(sf);
        const label = mf.label || nameNode.getText(sf);
        out.push(info(sf, `${stmt.name.text}.${label}`, m, mf.fn, nameNode));
      }
    } else {
      const vf = varFunction(stmt);
      if (vf) out.push(info(sf, vf.name.text, stmt, vf.fn, vf.name));
    }
  }
  return out;
}

// -- File metrics ---------------------------------------------------------------------
const OPERAND = new Set([
  ts.SyntaxKind.Identifier, ts.SyntaxKind.PrivateIdentifier, ts.SyntaxKind.NumericLiteral,
  ts.SyntaxKind.BigIntLiteral, ts.SyntaxKind.StringLiteral, ts.SyntaxKind.NoSubstitutionTemplateLiteral,
  ts.SyntaxKind.TemplateHead, ts.SyntaxKind.TemplateMiddle, ts.SyntaxKind.TemplateTail,
  ts.SyntaxKind.RegularExpressionLiteral,
]);

/** SLOC, comment lines and Halstead volume from the tokens of the syntax tree. */
function tokenMetrics(sf) {
  const code = new Set();
  const comments = new Set();
  const operators = new Set();
  const operands = new Set();
  let n1 = 0;
  let n2 = 0;
  const seenComments = new Set();
  const markComments = (pos) => {
    const ranges = [...(ts.getLeadingCommentRanges(sf.text, pos) || []), ...(ts.getTrailingCommentRanges(sf.text, pos) || [])];
    for (const r of ranges) {
      if (seenComments.has(r.pos)) continue;
      seenComments.add(r.pos);
      for (let l = lineOf(sf, r.pos); l <= lineOf(sf, r.end); l++) comments.add(l);
    }
  };
  const visit = (node) => {
    const kids = node.getChildren(sf);
    if (kids.length === 0 && node.kind !== ts.SyntaxKind.EndOfFileToken && node.kind !== ts.SyntaxKind.SyntaxList) {
      markComments(node.pos);
      for (let l = lineOf(sf, node.getStart(sf)); l <= lineOf(sf, node.end); l++) code.add(l);
      const text = node.getText(sf);
      if (OPERAND.has(node.kind)) {
        operands.add(text);
        n2++;
      } else {
        operators.add(text);
        n1++;
      }
      return;
    }
    if (node.kind === ts.SyntaxKind.EndOfFileToken) markComments(node.pos);
    kids.forEach(visit);
  };
  visit(sf);
  const vocabulary = operators.size + operands.size;
  const volume = vocabulary > 0 ? (n1 + n2) * Math.log2(vocabulary) : 0;
  let commentOnly = 0;
  for (const l of comments) if (!code.has(l)) commentOnly++;
  return { sloc: code.size, comments: commentOnly, volume };
}

/** Maintainability index as in radon (variant with comment share), 0–100 – same formula as for PHP. */
function mi(volume, cc, sloc, comments) {
  if (volume <= 0 || cc <= 0 || sloc <= 0) return 100;
  const pct = (comments * 100) / sloc;
  const raw = 171 - 5.2 * Math.log(volume) - 0.23 * cc - 16.2 * Math.log(sloc) + 50 * Math.sin(Math.sqrt(2.46 * (pct * Math.PI / 180)));
  return Math.round(Math.min(Math.max(0, (raw * 100) / 171), 100) * 100) / 100;
}

/** Package name of a module specifier: `lodash/fp` → `lodash`, `@scope/pkg/x` → `@scope/pkg`. */
function packageName(spec) {
  if (spec.startsWith('node:') || BUILTINS.has(spec)) return spec.startsWith('node:') ? spec : `node:${spec}`;
  const parts = spec.split('/');
  return spec.startsWith('@') ? parts.slice(0, 2).join('/') : parts[0];
}

const isRelative = (spec) => spec.startsWith('.') || spec.startsWith('/');

/** Module specifiers from import, export … from, require('…') and import('…'). */
function moduleSpecifiers(sf) {
  const specs = [];
  const visit = (n) => {
    if ((ts.isImportDeclaration(n) || ts.isExportDeclaration(n)) && n.moduleSpecifier && ts.isStringLiteral(n.moduleSpecifier)) {
      specs.push(n.moduleSpecifier.text);
    } else if (ts.isCallExpression(n) && n.arguments.length === 1 && ts.isStringLiteralLike(n.arguments[0])) {
      const callee = n.expression;
      if ((ts.isIdentifier(callee) && callee.text === 'require') || callee.kind === ts.SyntaxKind.ImportKeyword) {
        specs.push(n.arguments[0].text);
      }
    } else if (ts.isImportEqualsDeclaration(n) && ts.isExternalModuleReference(n.moduleReference)) {
      specs.push(n.moduleReference.expression.text);
    }
    ts.forEachChild(n, visit);
  };
  visit(sf);
  return specs;
}

/** Top-level statement that includes a module (import or `const x = require(…)`). */
function isImportStatement(stmt) {
  if (ts.isImportDeclaration(stmt) || ts.isImportEqualsDeclaration(stmt)) return true;
  if (!ts.isVariableStatement(stmt)) return false;
  return stmt.declarationList.declarations.every((d) => {
    let init = d.initializer;
    while (init && ts.isPropertyAccessExpression(init)) init = init.expression;
    return init && ts.isCallExpression(init) && ts.isIdentifier(init.expression) && init.expression.text === 'require';
  });
}

/** Map a relative import to a file in the repository (with and without extension, index files). */
function resolveRelative(fromRel, spec, root) {
  if (!fromRel || !root) return null;
  const base = path.posix.normalize(path.posix.join(path.posix.dirname(fromRel), spec));
  const stem = base.replace(/\.(m|c)?js$/, '');
  const tries = [base, ...SOURCE_EXT.map((e) => stem + e), ...SOURCE_EXT.map((e) => `${base}/index${e}`)];
  return tries.find((t) => fs.existsSync(path.join(root, t)) && fs.statSync(path.join(root, t)).isFile()) || null;
}

function fileInfo(sf, fns, rel, root) {
  const imports = [];
  let insertAfter = 0;
  for (const stmt of sf.statements) {
    if (isImportStatement(stmt)) {
      imports.push(normalize(stmt.getText(sf)));
      insertAfter = lineOf(sf, stmt.end);
    } else if (ts.isExpressionStatement(stmt) && ts.isStringLiteral(stmt.expression) && insertAfter === 0 && imports.length === 0) {
      insertAfter = lineOf(sf, stmt.end); // 'use strict' – imports after it
    } else if (!ts.isExpressionStatement(stmt)) {
      // Imports after the first other code do not count for the insertion point.
      if (imports.length) break;
    }
  }
  if (insertAfter === 0 && sf.text.startsWith('#!')) insertAfter = 1;
  const specs = moduleSpecifiers(sf);
  const modules = [...new Set(specs.filter((s) => !isRelative(s)).map(packageName))].sort();
  const referenced = [...new Set(specs.filter(isRelative).map((s) => resolveRelative(rel, s, root)).filter(Boolean))];
  const tm = tokenMetrics(sf);
  const cc = fns.reduce((a, f) => a + f.complexity, 0);
  return {
    uses: imports,
    use_insert_after: insertAfter,
    modules,
    builtin_modules: modules.filter((m) => m.startsWith('node:')),
    declared: rel ? [rel] : [],
    referenced,
    sloc: tm.sloc,
    mi: mi(tm.volume, Math.max(cc, 1), tm.sloc, tm.comments),
  };
}

/** Texts of all tokens in order – without whitespace and comments (comparison form). */
function leafTokens(sf) {
  const out = [];
  const visit = (node) => {
    const kids = node.getChildren(sf);
    if (kids.length === 0) {
      if (node.kind !== ts.SyntaxKind.EndOfFileToken && node.kind !== ts.SyntaxKind.SyntaxList) out.push(node.getText(sf));
      return;
    }
    kids.forEach(visit);
  };
  visit(sf);
  return out;
}

// -- Operations ----------------------------------------------------------------------
function parseFunction(code, method) {
  if (method) {
    const sf = parse(`class __JouleCodeWrapper__ {\n${code}\n}\n`);
    const cls = sf.statements[0];
    if (sf.statements.length !== 1 || !ts.isClassDeclaration(cls)) {
      throw new Error('Expected exactly one method without further code.');
    }
    const members = cls.members.filter((m) => !ts.isSemicolonClassElement(m));
    const mf = members.length === 1 ? memberFunction(members[0]) : null;
    if (!mf) throw new Error('Expected exactly one method without further class members.');
    return { name: mf.label || mf.name.getText(sf) };
  }
  const sf = parse(code);
  const stmts = sf.statements;
  if (stmts.length === 1 && ts.isFunctionDeclaration(stmts[0]) && stmts[0].name) {
    return { name: stmts[0].name.text };
  }
  const vf = stmts.length === 1 ? varFunction(stmts[0]) : null;
  if (!vf) throw new Error('Expected exactly one function definition without further code.');
  return { name: vf.name.text };
}

function handle(req) {
  switch (req.op) {
    case 'functions':
      return { functions: functions(parse(req.source, req.path)) };
    case 'lint':
      parse(req.source, req.path);
      return {};
    case 'canonical':
      return { canonical: leafTokens(parse(req.source, req.path)).join('\0') };
    case 'parse_function':
      return parseFunction(req.code, Boolean(req.method));
    case 'file_info': {
      const sf = parse(req.source, req.path);
      return fileInfo(sf, functions(sf), req.path, req.root);
    }
    case 'analyze': {
      const files = {};
      for (const rel of req.files) {
        const source = fs.readFileSync(path.join(req.root, rel), 'utf8');
        try {
          const sf = parse(source, rel);
          const fns = functions(sf);
          files[rel] = { functions: fns, info: fileInfo(sf, fns, rel, req.root) };
        } catch (e) {
          files[rel] = { error: e.message };
        }
      }
      return { files, builtins: [...BUILTINS].filter((b) => !b.startsWith('node:')) };
    }
    default:
      throw new Error(`Unknown operation: ${req.op}`);
  }
}

let input = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', (c) => (input += c));
process.stdin.on('end', () => {
  let res;
  try {
    res = { ok: true, ...handle(JSON.parse(input)) };
  } catch (e) {
    res = { ok: false, error: e.message };
  }
  process.stdout.write(JSON.stringify(res));
});
