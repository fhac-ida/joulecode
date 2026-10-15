/**
 * Notifications: mentions in internal notes, daily digest per agent.
 */
import { escapeRegExp, truncate } from './text.js';
import { isOpen } from './tickets.js';

/**
 * Mentions “@name” (case-insensitive, whole word) in internal notes from `since` on.
 * @returns {Array<{ agent, ticket, at }>} by time, then ticket, then agent
 */
export function mentions(tickets, agents, since) {
  const out = [];
  for (const t of tickets) {
    for (const m of t.messages) {
      if (!m.internal || m.at < since) continue;
      for (const a of agents) {
        const pattern = new RegExp(`(^|\\W)@${escapeRegExp(a.name)}(?![\\w-])`, 'i');
        if (pattern.test(m.body)) {
          out.push({ agent: a.id, ticket: t.id, at: m.at });
        }
      }
    }
  }
  return out.sort((x, y) => x.at - y.at || x.ticket - y.ticket || (x.agent < y.agent ? -1 : x.agent > y.agent ? 1 : 0));
}

/** Daily digest for an agent as text. */
export function digest(agent, tickets, slaByTicket, mentionList) {
  const mine = tickets.filter((t) => t.assignee === agent.id && isOpen(t));
  const lines = [`Good morning ${agent.name},`, '', `you have ${mine.length} open tickets.`];
  const critical = mine.filter((t) => slaByTicket.get(t.id)?.state === 'breached');
  const warning = mine.filter((t) => slaByTicket.get(t.id)?.state === 'warning');
  if (critical.length) {
    lines.push('', 'SLA breached:');
    for (const t of critical) lines.push(`  #${t.id} ${truncate(t.subject, 50)}`);
  }
  if (warning.length) {
    lines.push('', 'SLA expiring soon:');
    for (const t of warning) lines.push(`  #${t.id} ${truncate(t.subject, 50)}`);
  }
  const mentioned = mentionList.filter((m) => m.agent === agent.id);
  if (mentioned.length) {
    lines.push('', `You were mentioned ${mentioned.length}×: ${[...new Set(mentioned.map((m) => `#${m.ticket}`))].join(', ')}`);
  }
  return lines.join('\n') + '\n';
}
