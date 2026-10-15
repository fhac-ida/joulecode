/**
 * Knowledge base: suggest matching articles for a ticket (TF-IDF over title and text).
 */
import { tokenize } from './text.js';

const STOPWORDS = new Set(['the', 'a', 'an', 'and', 'or', 'is', 'not', 'with', 'at', 'i', 'we', 'they', 'it', 'to', 'in', 'on', 'for', 'of', 'from', 'my', 'our', 'this', 'that', 'be']);

function terms(text) {
  return tokenize(text).filter((w) => !STOPWORDS.has(w));
}

export class KnowledgeBase {
  /** @param {Array<{ id, title, body, tags }>} articles */
  constructor(articles = []) {
    this.articles = [];
    this.df = new Map();
    this.vectors = new Map();
    for (const a of articles) this.add(a);
  }

  add(article) {
    if (this.vectors.has(article.id)) {
      throw new Error(`article ${article.id} already exists`);
    }
    const tf = new Map();
    for (const w of [...terms(article.title), ...terms(article.title), ...terms(article.body)]) {
      tf.set(w, (tf.get(w) ?? 0) + 1);
    }
    for (const w of tf.keys()) this.df.set(w, (this.df.get(w) ?? 0) + 1);
    this.articles.push(article);
    this.vectors.set(article.id, tf);
  }

  idf(word) {
    const df = this.df.get(word) ?? 0;
    return Math.log((1 + this.articles.length) / (1 + df)) + 1;
  }

  /** Up to `limit` articles with similarity ≥ `minScore`, best first (tie: ID). */
  suggest(text, limit = 3, minScore = 0.1) {
    const query = new Map();
    for (const w of terms(text)) query.set(w, (query.get(w) ?? 0) + 1);
    const qNorm = Math.sqrt([...query].reduce((s, [w, n]) => s + (n * this.idf(w)) ** 2, 0));
    if (qNorm === 0) return [];
    const scored = [];
    for (const a of this.articles) {
      const v = this.vectors.get(a.id);
      let dot = 0;
      let norm = 0;
      for (const [w, n] of v) {
        const weight = n * this.idf(w);
        norm += weight ** 2;
        const q = query.get(w);
        if (q) dot += weight * q * this.idf(w);
      }
      const score = dot / (Math.sqrt(norm) * qNorm);
      if (score >= minScore) scored.push({ id: a.id, title: a.title, score: Math.round(score * 1000) / 1000 });
    }
    return scored.sort((x, y) => y.score - x.score || (x.id < y.id ? -1 : 1)).slice(0, limit);
  }

  /** Articles that match a tag. */
  byTag(tag) {
    return this.articles.filter((a) => (a.tags ?? []).includes(tag)).map((a) => a.id);
  }
}
