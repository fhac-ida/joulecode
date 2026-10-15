/**
 * joule:code – analyses V8 CPU profiles (`node --cpu-prof`) in the benchmark container.
 *
 * Usage: node cpuprofile.js <directory> '<json list of excluded paths>'
 * Output: one line "JOULECODE_JSON{...}" with self and total time per function of the
 * project files (relative to the working directory). Without dependencies because it runs in
 * the user's image.
 *
 * Built-in functions (Array.prototype.includes, Map#get … – in the profile without a file or
 * with a `node:` URL) count towards the calling project function, like `x in some_list` in cProfile
 * and as in the PHP back-end. The profiler samples; there are no call counts.
 */
'use strict';
const fs = require('fs');
const path = require('path');

// Can be called as a file (`node cpuprofile.js …`) or via `node -e "<script>" …`.
const args = process.argv.slice(require.main ? 2 : 1);
const dir = args[0];
const excluded = JSON.parse(args[1] || '[]').map((e) => e.replace(/\/+$/, ''));
const root = process.cwd().replace(/\/+$/, '');
const SPECIAL = new Set(['(root)', '(program)', '(idle)', '(garbage collector)']);

function projectFile(url) {
  if (!url) return null;
  let p = url.startsWith('file://') ? decodeURIComponent(new URL(url).pathname) : url;
  if (!p.startsWith(root + '/')) return null;
  const rel = p.slice(root.length + 1);
  if (rel.startsWith('node_modules/') || rel.includes('/node_modules/')) return null;
  if (excluded.some((e) => rel === e || rel.startsWith(e + '/'))) return null;
  return rel;
}
const builtin = (cf) => !SPECIAL.has(cf.functionName) && (!cf.url || cf.url.startsWith('node:'));

const stats = new Map(); // key → {file, line, function, self, incl, samples}
let total = 0;

for (const name of fs.existsSync(dir) ? fs.readdirSync(dir) : []) {
  if (!name.endsWith('.cpuprofile')) continue;
  const prof = JSON.parse(fs.readFileSync(path.join(dir, name), 'utf8'));
  const byId = new Map(prof.nodes.map((n) => [n.id, n]));
  const parent = new Map();
  for (const n of prof.nodes) for (const c of n.children || []) parent.set(c, n.id);
  const keyOf = new Map();
  for (const n of prof.nodes) {
    const file = projectFile(n.callFrame.url);
    if (!file) continue;
    const fn = n.callFrame.functionName || '(anonymous)';
    const line = n.callFrame.lineNumber + 1;
    const key = `${file}\0${fn}\0${line}`;
    keyOf.set(n.id, key);
    if (!stats.has(key)) stats.set(key, { file, line, function: fn, self: 0, incl: 0, samples: 0 });
  }
  const samples = prof.samples || [];
  const deltas = prof.timeDeltas || [];
  for (let i = 0; i < samples.length; i++) {
    // The time until the next sample belongs to this one.
    const dt = (i + 1 < deltas.length ? deltas[i + 1] : deltas[i] || 0) / 1e6;
    let node = byId.get(samples[i]);
    if (!node || node.callFrame.functionName === '(idle)') continue;
    total += dt;
    // Self time: the project function itself or – for built-in functions – its caller.
    let id = node.id;
    while (id !== undefined && !keyOf.has(id) && builtin(byId.get(id).callFrame)) id = parent.get(id);
    if (id !== undefined && keyOf.has(id)) {
      const s = stats.get(keyOf.get(id));
      s.self += dt;
      s.samples++;
    }
    // Total time: every project function on the stack once (recursion not counted twice).
    const seen = new Set();
    for (let cur = node.id; cur !== undefined; cur = parent.get(cur)) {
      const k = keyOf.get(cur);
      if (k && !seen.has(k)) {
        seen.add(k);
        stats.get(k).incl += dt;
      }
    }
  }
}

const rows = [...stats.values()].filter((s) => s.incl > 0).map((s) => ({
  file: s.file, line: s.line, function: s.function, ncalls: 0, samples: s.samples, tottime: s.self, cumtime: s.incl,
}));
process.stdout.write('JOULECODE_JSON' + JSON.stringify({ total_s: total, functions: rows }) + '\n');
