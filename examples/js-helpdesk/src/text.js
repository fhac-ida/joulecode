/**
 * Text processing for subject lines, search and output.
 */

const REPLY_PREFIX = /^\s*((re|aw|antw|fwd?|wg)\s*(\[\d+\])?\s*:\s*)+/i;
const TICKET_REF = /\[#(\d+)\]/;

/** Subject without “Re:”, “AW:”, “Fwd:” etc. (English and German reply prefixes), whitespace collapsed. */
export function normalizeSubject(subject) {
  return subject.replace(REPLY_PREFIX, '').replace(/\s+/g, ' ').trim();
}

/** Ticket number from “[#1234]” in the subject, or null. */
export function ticketRef(subject) {
  const m = TICKET_REF.exec(subject);
  return m ? Number(m[1]) : null;
}

/** Lower case, German umlauts spelled out, only [a-z0-9-]. */
export function slugify(text) {
  return text
    .toLowerCase()
    .replace(/ä/g, 'ae')
    .replace(/ö/g, 'oe')
    .replace(/ü/g, 'ue')
    .replace(/ß/g, 'ss')
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '');
}

/** Words for the search: lower case, at least two characters. */
export function tokenize(text) {
  return text
    .toLowerCase()
    .split(/[^\p{L}\p{N}]+/u)
    .filter((w) => w.length >= 2);
}

/** Simple Markdown (headings, bold, links, lists) as plain text. */
export function markdownToText(md) {
  return md
    .replace(/^#{1,6}\s*/gm, '')
    .replace(/\*\*(.+?)\*\*/g, '$1')
    .replace(/\[([^\]]+)\]\([^)]+\)/g, '$1')
    .replace(/^\s*[-*]\s+/gm, '• ')
    .trim();
}

/** Shorten text to `max` characters, at a word boundary, with “…”. */
export function truncate(text, max) {
  if (text.length <= max) {
    return text;
  }
  const cut = text.slice(0, max - 1);
  const space = cut.lastIndexOf(' ');
  return (space > max / 2 ? cut.slice(0, space) : cut) + '…';
}

/** Escape for regular expressions. */
export function escapeRegExp(text) {
  return text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

/** Escape a CSV cell (semicolon-separated). */
export function csvCell(value) {
  const text = value === null || value === undefined ? '' : String(value);
  return /[;"\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
}
