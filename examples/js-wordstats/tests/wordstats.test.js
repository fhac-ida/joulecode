import assert from 'node:assert/strict';
import { test } from 'node:test';

import { frequentWords, report, uniqueWords } from '../src/wordstats.js';

const TEXT = 'The dog and a cat and the bird and THE fish';

test('uniqueWords keeps the order of first occurrence', () => {
  assert.deepEqual(uniqueWords(TEXT), ['the', 'dog', 'and', 'a', 'cat', 'bird', 'fish']);
});

test('uniqueWords with empty text', () => {
  assert.deepEqual(uniqueWords(''), []);
});

test('uniqueWords: numeric words stay in place', () => {
  // An object used as a counter moves integer keys to the front – the order must be kept.
  assert.deepEqual(uniqueWords('b 10 a 9 10'), ['b', '10', 'a', '9']);
});

test('frequentWords counts and breaks ties alphabetically', () => {
  assert.deepEqual(frequentWords(TEXT, 3), [['and', 3], ['the', 3], ['a', 1]]);
});

test('frequentWords: numeric words stay strings', () => {
  assert.deepEqual(frequentWords('10 9 9 10 b', 5), [['10', 2], ['9', 2], ['b', 1]]);
});

test('frequentWords with top larger than the vocabulary', () => {
  assert.equal(frequentWords('a b a', 10).length, 2);
});

test('report', () => {
  assert.equal(report('b a b', 2), 'Total words: 3\nDistinct words: 2\n  b: 2\n  a: 1\n');
});
