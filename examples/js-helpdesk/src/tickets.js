/**
 * Tickets: creation, status changes, sorting in e-mails (threading), merging
 * duplicates, search with filters and sorting.
 *
 * Times are milliseconds since 1970 (UTC). A ticket:
 *
 *   { id, subject, requester, status, priority, tags, assignee, team,
 *     createdAt, updatedAt, firstResponseAt, resolvedAt, messages: [{ from, at, body, internal }] }
 */
import { normalizeSubject, ticketRef, tokenize } from './text.js';

export const STATUSES = ['new', 'open', 'pending', 'on_hold', 'solved', 'closed'];
export const PRIORITIES = ['low', 'normal', 'high', 'urgent'];

const TRANSITIONS = {
  new: ['open', 'pending', 'solved', 'closed'],
  open: ['pending', 'on_hold', 'solved'],
  pending: ['open', 'solved'],
  on_hold: ['open', 'solved'],
  solved: ['open', 'closed'],
  closed: [],
};

const HOUR = 3600 * 1000;
const DAY = 24 * HOUR;
const THREAD_WINDOW = 7 * DAY;
const DUPLICATE_WINDOW = HOUR;

export class TicketError extends Error {}

export function createTicket(fields) {
  const { id, subject, requester, createdAt } = fields;
  if (!Number.isInteger(id) || id <= 0) {
    throw new TicketError(`invalid ticket number ${id}`);
  }
  if (!subject || !requester) {
    throw new TicketError(`ticket ${id}: subject and requester are mandatory`);
  }
  const priority = fields.priority ?? 'normal';
  if (!PRIORITIES.includes(priority)) {
    throw new TicketError(`ticket ${id}: unknown priority ${priority}`);
  }
  return {
    id,
    subject,
    requester: requester.toLowerCase(),
    status: fields.status ?? 'new',
    priority,
    tags: [...(fields.tags ?? [])],
    assignee: fields.assignee ?? null,
    team: fields.team ?? null,
    createdAt,
    updatedAt: createdAt,
    firstResponseAt: null,
    resolvedAt: null,
    messages: [...(fields.messages ?? [])],
  };
}

/** Status change according to the allowed transitions; sets resolvedAt when solving. */
export function transition(ticket, to, at) {
  if (ticket.status === to) {
    return ticket;
  }
  if (!TRANSITIONS[ticket.status]?.includes(to)) {
    throw new TicketError(`ticket ${ticket.id}: ${ticket.status} → ${to} is not allowed`);
  }
  ticket.status = to;
  ticket.updatedAt = at;
  if (to === 'solved') {
    ticket.resolvedAt = at;
  } else if (to === 'open') {
    ticket.resolvedAt = null;
  }
  return ticket;
}

export function priorityRank(priority) {
  return PRIORITIES.indexOf(priority);
}

/** Ticket for the number, or undefined. */
export function findTicket(tickets, id) {
  return tickets.find((t) => t.id === id);
}

export function isOpen(ticket) {
  return ticket.status !== 'solved' && ticket.status !== 'closed';
}

/** Reply of an agent: sets the first response time and opens new tickets. */
export function addAgentReply(ticket, agent, at, body, internal = false) {
  ticket.messages.push({ from: agent, at, body, internal });
  ticket.updatedAt = at;
  if (!internal && ticket.firstResponseAt === null) {
    ticket.firstResponseAt = at;
  }
  if (!internal && ticket.status === 'new') {
    ticket.status = 'open';
  }
  return ticket;
}

// -- E-Mails einsortieren -----------------------------------------------------------

/**
 * Assign incoming e-mails to tickets.
 *
 * Order of assignment:
 *   1. “[#123]” in the subject and ticket 123 exists → this ticket;
 *   2. otherwise: open ticket of the same sender with the same (normalised) subject,
 *      last updated at most 7 days before the mail – the most recently updated one;
 *   3. otherwise: new ticket (number = highest so far + 1).
 *
 * Mails are processed in the order of their arrival (`at`, on a tie in the order of the
 * list). A mail on a solved ticket reopens it; a mail on a
 * closed one creates a new ticket with a reference.
 *
 * @returns {{ tickets: object[], created: number[], reopened: number[] }}
 */
export function threadMessages(tickets, mails) {
  const all = [...tickets];
  const created = [];
  const reopened = [];
  let nextId = all.reduce((max, t) => Math.max(max, t.id), 0) + 1;
  const ordered = mails.map((m, i) => [m, i]).sort((a, b) => a[0].at - b[0].at || a[1] - b[1]);
  for (const [mail, ] of ordered) {
    const from = mail.from.toLowerCase();
    let ticket;
    const ref = ticketRef(mail.subject);
    if (ref !== null) {
      ticket = findTicket(all, ref);
    }
    if (!ticket) {
      const subject = normalizeSubject(mail.subject);
      const candidates = all.filter(
        (t) =>
          t.requester === from &&
          isOpen(t) &&
          normalizeSubject(t.subject) === subject &&
          mail.at - t.updatedAt <= THREAD_WINDOW &&
          t.updatedAt <= mail.at,
      );
      ticket = candidates.reduce((best, t) => (best === undefined || t.updatedAt > best.updatedAt ? t : best), undefined);
    }
    if (ticket && ticket.status === 'closed') {
      const follow = createTicket({
        id: nextId++,
        subject: normalizeSubject(mail.subject),
        requester: from,
        createdAt: mail.at,
        priority: ticket.priority,
        tags: [...ticket.tags, `follow-up-of-${ticket.id}`],
        messages: [{ from, at: mail.at, body: mail.body, internal: false }],
      });
      all.push(follow);
      created.push(follow.id);
      continue;
    }
    if (!ticket) {
      ticket = createTicket({
        id: nextId++,
        subject: normalizeSubject(mail.subject),
        requester: from,
        createdAt: mail.at,
        priority: guessPriority(mail),
        tags: guessTags(mail),
      });
      all.push(ticket);
      created.push(ticket.id);
    } else if (ticket.status === 'solved') {
      transition(ticket, 'open', mail.at);
      reopened.push(ticket.id);
    } else if (ticket.status === 'pending') {
      transition(ticket, 'open', mail.at);
    }
    ticket.messages.push({ from, at: mail.at, body: mail.body, internal: false });
    ticket.updatedAt = Math.max(ticket.updatedAt, mail.at);
  }
  return { tickets: all, created, reopened };
}

const URGENT_WORDS = ['urgent', 'urgently', 'outage', 'down', 'asap'];
const TAG_WORDS = {
  printer: 'hardware',
  laptop: 'hardware',
  password: 'access',
  login: 'access',
  vpn: 'network',
  wifi: 'network',
  invoice: 'billing',
  licence: 'software',
};

export function guessPriority(mail) {
  const words = tokenize(`${mail.subject} ${mail.body}`);
  if (words.some((w) => URGENT_WORDS.includes(w))) {
    return 'high';
  }
  return 'normal';
}

/** Keywords from subject and text; every tag at most once, in order of discovery. */
export function guessTags(mail) {
  const tags = [];
  for (const word of tokenize(`${mail.subject} ${mail.body}`)) {
    const tag = TAG_WORDS[word];
    if (tag && !tags.includes(tag)) {
      tags.push(tag);
    }
  }
  return tags;
}

// -- Duplicates -----------------------------------------------------------------------

/**
 * Tickets of the same sender with the same subject, created within one hour,
 * are merged into the older one (messages by time, stable; tags united).
 * The younger ticket is closed and gets the tag `duplicate-of-<id>`.
 *
 * @returns {Array<[number, number]>} pairs (kept, closed)
 */
export function mergeDuplicates(tickets) {
  const merged = [];
  const byKey = new Map();
  const sorted = [...tickets].sort((a, b) => a.createdAt - b.createdAt || a.id - b.id);
  for (const t of sorted) {
    if (!isOpen(t)) {
      continue;
    }
    const key = `${t.requester}\u0000${normalizeSubject(t.subject).toLowerCase()}`;
    const keep = byKey.get(key);
    if (keep && t.createdAt - keep.createdAt <= DUPLICATE_WINDOW) {
      keep.messages = [...keep.messages, ...t.messages].sort((a, b) => a.at - b.at);
      for (const tag of t.tags) {
        if (!keep.tags.includes(tag)) {
          keep.tags.push(tag);
        }
      }
      if (priorityRank(t.priority) > priorityRank(keep.priority)) {
        keep.priority = t.priority;
      }
      t.status = 'closed';
      t.tags.push(`duplicate-of-${keep.id}`);
      merged.push([keep.id, t.id]);
    } else {
      byKey.set(key, t);
    }
  }
  return merged;
}

/** All tags in the order of their first occurrence (case-sensitive). */
export function uniqueTags(tickets) {
  const out = [];
  for (const t of tickets) {
    for (const tag of t.tags) {
      if (!out.includes(tag)) {
        out.push(tag);
      }
    }
  }
  return out;
}

/** How often does every tag occur? Most frequent first, on a tie in order of first occurrence. */
export function tagCounts(tickets) {
  const tags = uniqueTags(tickets);
  const counts = tags.map((tag) => [tag, tickets.filter((t) => t.tags.includes(tag)).length]);
  return counts.sort((a, b) => b[1] - a[1]);
}

// -- Search -----------------------------------------------------------------------------

/**
 * @param {object} query { text, status: [], priority: [], assignee, team, tags: [], unassigned,
 *                         createdFrom, createdTo, sort: 'updated'|'created'|'priority'|'relevance',
 *                         page, perPage }
 */
export function searchTickets(tickets, query = {}) {
  const words = query.text ? tokenize(query.text) : [];
  const hits = [];
  for (const t of tickets) {
    if (query.status?.length && !query.status.includes(t.status)) continue;
    if (query.priority?.length && !query.priority.includes(t.priority)) continue;
    if (query.assignee !== undefined && t.assignee !== query.assignee) continue;
    if (query.unassigned && t.assignee !== null) continue;
    if (query.team !== undefined && t.team !== query.team) continue;
    if (query.createdFrom !== undefined && t.createdAt < query.createdFrom) continue;
    if (query.createdTo !== undefined && t.createdAt >= query.createdTo) continue;
    if (query.tags?.length && !query.tags.every((tag) => t.tags.includes(tag))) continue;
    const score = words.length ? relevance(t, words) : 0;
    if (words.length && score === 0) continue;
    hits.push({ ticket: t, score });
  }
  const sort = query.sort ?? (words.length ? 'relevance' : 'updated');
  const cmp = {
    updated: (a, b) => b.ticket.updatedAt - a.ticket.updatedAt || a.ticket.id - b.ticket.id,
    created: (a, b) => a.ticket.createdAt - b.ticket.createdAt || a.ticket.id - b.ticket.id,
    priority: (a, b) =>
      priorityRank(b.ticket.priority) - priorityRank(a.ticket.priority) || a.ticket.createdAt - b.ticket.createdAt || a.ticket.id - b.ticket.id,
    relevance: (a, b) => b.score - a.score || b.ticket.updatedAt - a.ticket.updatedAt || a.ticket.id - b.ticket.id,
  }[sort];
  if (!cmp) {
    throw new TicketError(`unknown sort order ${sort}`);
  }
  hits.sort(cmp);
  const perPage = Math.max(1, query.perPage ?? 25);
  const page = Math.max(1, query.page ?? 1);
  return {
    total: hits.length,
    pages: Math.ceil(hits.length / perPage),
    items: hits.slice((page - 1) * perPage, page * perPage).map((h) => h.ticket),
  };
}

/** Relevance: subject three times, tags twice, message text once per word. */
function relevance(ticket, words) {
  const subject = tokenize(ticket.subject);
  const body = new Set(ticket.messages.flatMap((m) => (m.internal ? [] : tokenize(m.body))));
  let score = 0;
  for (const w of words) {
    if (subject.includes(w)) score += 3;
    if (ticket.tags.includes(w)) score += 2;
    if (body.has(w)) score += 1;
  }
  return score;
}

// -- Overviews ------------------------------------------------------------------------------

/** Open tickets by age in days: 0–1, 2–3, 4–7, 8–30, > 30. */
export function agingBuckets(tickets, now) {
  const buckets = { '0-1': 0, '2-3': 0, '4-7': 0, '8-30': 0, '>30': 0 };
  for (const t of tickets) {
    if (!isOpen(t)) continue;
    const days = Math.floor((now - t.createdAt) / DAY);
    if (days <= 1) buckets['0-1']++;
    else if (days <= 3) buckets['2-3']++;
    else if (days <= 7) buckets['4-7']++;
    else if (days <= 30) buckets['8-30']++;
    else buckets['>30']++;
  }
  return buckets;
}

/** Tickets without a response from the team for `hours` hours (open, last message from the customer). */
export function waitingForAgent(tickets, now, hours = 24) {
  return tickets
    .filter((t) => {
      if (!isOpen(t) || t.status === 'pending') return false;
      const last = t.messages.at(-1);
      return last !== undefined && last.from === t.requester && now - last.at >= hours * HOUR;
    })
    .sort((a, b) => a.messages.at(-1).at - b.messages.at(-1).at || a.id - b.id);
}

/** Tickets that are closed automatically: solved and unchanged for `days` days. */
export function autoClose(tickets, now, days = 5) {
  const closed = [];
  for (const t of tickets) {
    if (t.status === 'solved' && now - t.updatedAt >= days * DAY) {
      transition(t, 'closed', now);
      closed.push(t.id);
    }
  }
  return closed;
}

/** Status overview in the fixed order of the statuses. */
export function statusCounts(tickets) {
  const counts = Object.fromEntries(STATUSES.map((s) => [s, 0]));
  for (const t of tickets) {
    counts[t.status]++;
  }
  return counts;
}

// -- Bulk editing and checks -----------------------------------------------------------------

/** Assign several tickets to an agent; unknown numbers are reported. */
export function bulkAssign(tickets, ids, assignee, at) {
  const missing = [];
  for (const id of ids) {
    const t = findTicket(tickets, id);
    if (!t) {
      missing.push(id);
      continue;
    }
    t.assignee = assignee;
    t.updatedAt = at;
  }
  return missing;
}

/** Add a tag to several tickets (only where it is still missing); number of changed tickets. */
export function bulkTag(tickets, ids, tag, at) {
  let changed = 0;
  for (const id of ids) {
    const t = findTicket(tickets, id);
    if (t && !t.tags.includes(tag)) {
      t.tags.push(tag);
      t.updatedAt = at;
      changed++;
    }
  }
  return changed;
}

/** Timeline of a ticket: messages and status events, by time. */
export function timeline(ticket) {
  const events = ticket.messages.map((m) => ({ at: m.at, kind: m.internal ? 'note' : 'message', who: m.from }));
  events.push({ at: ticket.createdAt, kind: 'created', who: ticket.requester });
  if (ticket.firstResponseAt !== null) events.push({ at: ticket.firstResponseAt, kind: 'first-response', who: ticket.assignee });
  if (ticket.resolvedAt !== null) events.push({ at: ticket.resolvedAt, kind: 'solved', who: ticket.assignee });
  return events.sort((a, b) => a.at - b.at);
}

/** Plausibility check of the tickets (duplicate numbers, times, statuses). */
export function validateTickets(tickets) {
  const problems = [];
  const ids = new Set();
  for (const t of tickets) {
    if (ids.has(t.id)) problems.push(`ticket ${t.id}: duplicate number`);
    ids.add(t.id);
    if (!STATUSES.includes(t.status)) problems.push(`ticket ${t.id}: unknown status ${t.status}`);
    if (t.updatedAt < t.createdAt) problems.push(`ticket ${t.id}: changed before creation`);
    if (t.resolvedAt !== null && t.resolvedAt < t.createdAt) problems.push(`ticket ${t.id}: solved before creation`);
    if (t.status === 'solved' && t.resolvedAt === null) problems.push(`ticket ${t.id}: solved without a timestamp`);
  }
  return problems;
}
