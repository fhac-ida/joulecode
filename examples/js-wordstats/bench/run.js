/**
 * Benchmark: fixed, deterministic amount of work.
 *
 * The energy for exactly this work is measured. BENCH_ROUNDS scales the run time
 * (on the measurement machine, choose it so that a run takes >= 20 s).
 */
import { frequentWords, report, uniqueWords } from '../src/wordstats.js';

const ROUNDS = Number(process.env.BENCH_ROUNDS ?? 40);
const VOCAB = Number(process.env.BENCH_VOCAB ?? 1500);
const WORDS = Number(process.env.BENCH_WORDS ?? 6000);

// Fixed random generator (mulberry32) so that every variant gets the same texts.
let seed = 1234;
const random = () => {
  seed = (seed + 0x6d2b79f5) | 0;
  let t = Math.imul(seed ^ (seed >>> 15), 1 | seed);
  t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
  return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
};

const vocabulary = Array.from({ length: VOCAB }, (_, i) => `word${i}`);
const texts = Array.from({ length: 5 }, () =>
  Array.from({ length: WORDS }, () => vocabulary[Math.floor(random() * VOCAB)]).join(' '),
);

const start = process.hrtime.bigint();
let checksum = 0;
for (let i = 0; i < ROUNDS; i++) {
  const text = texts[i % texts.length];
  checksum += uniqueWords(text).length;
  checksum += frequentWords(text, 20).reduce((sum, [, n]) => sum + n, 0);
  checksum += report(text, 10).length;
}
const seconds = Number(process.hrtime.bigint() - start) / 1e9;
console.log(`rounds=${ROUNDS} checksum=${checksum} seconds=${seconds.toFixed(2)}`);
