/**
 * Text statistics (original variant with typical inefficiencies).
 */

/**
 * Words of the text, lower-cased, split at whitespace.
 *
 * @param {string} text
 * @returns {string[]}
 */
const words = (text) => text.toLowerCase().split(/\s+/).filter(Boolean);

/**
 * All words in order of their first occurrence, lower-cased.
 *
 * @param {string} text
 * @returns {string[]}
 */
export function uniqueWords(text) {
  const seen = [];
  for (const word of words(text)) {
    if (!seen.includes(word)) { // search in an array: O(n)
      seen.push(word);
    }
  }
  return seen;
}

/**
 * The `top` most frequent words with their count, ties sorted alphabetically.
 *
 * @param {string} text
 * @param {number} [top=10]
 * @returns {Array<[string, number]>}
 */
export function frequentWords(text, top = 10) {
  const all = words(text);
  const counts = [];
  for (const word of uniqueWords(text)) {
    let n = 0;
    for (const other of all) { // nested loop: O(n*m)
      if (other === word) {
        n++;
      }
    }
    counts.push([word, n]);
  }
  counts.sort((a, b) => b[1] - a[1] || (a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : 0));
  return counts.slice(0, top);
}

/**
 * Short text report.
 *
 * @param {string} text
 * @param {number} [top=5]
 * @returns {string}
 */
export function report(text, top = 5) {
  let out = '';
  out += `Total words: ${words(text).length}\n`;
  out += `Distinct words: ${uniqueWords(text).length}\n`;
  for (const [word, n] of frequentWords(text, top)) {
    out += `  ${word}: ${n}\n`;
  }
  return out;
}
