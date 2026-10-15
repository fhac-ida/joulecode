/**
 * js-helpdesk – ticket system (joule:code example with a lot of code).
 *
 * `processDay` simulates a working day: sort in mails, merge duplicates,
 * assign, evaluate SLAs, generate mentions and reports.
 */
import { runAutomations } from './automation.js';
import { KnowledgeBase } from './knowledge.js';
import { mentions, digest } from './notify.js';
import { autoAssign, releaseOffline } from './routing.js';
import { slaCompliance, slaStatus } from './sla.js';
import { agentStats, dailyFlow, ticketsCsv, weeklyReport } from './stats.js';
import { isOpen, mergeDuplicates, tagCounts, threadMessages, uniqueTags, waitingForAgent } from './tickets.js';

export * from './text.js';
export * from './tickets.js';
export * from './sla.js';
export * from './routing.js';
export * from './notify.js';
export * from './stats.js';
export * from './automation.js';
export * from './knowledge.js';
export * from './importer.js';

export function processDay(tickets, agents, mails, now, { rules = [], articles = [] } = {}) {
  const threaded = threadMessages(tickets, mails);
  const all = threaded.tickets;
  const merged = mergeDuplicates(all);
  const automations = runAutomations(all, rules, now);
  const kb = new KnowledgeBase(articles);
  const suggestions = new Map(
    threaded.created.map((id) => {
      const t = all.find((x) => x.id === id);
      return [id, kb.suggest(`${t.subject} ${t.messages.map((m) => m.body).join(' ')}`)];
    }),
  );
  const released = releaseOffline(all, agents, now);
  const assigned = autoAssign(all, agents, now);
  const sla = new Map(all.filter(isOpen).map((t) => [t.id, slaStatus(t, now)]));
  const mentionList = mentions(all, agents, now - 24 * 3600 * 1000);
  const digests = agents.filter((a) => a.online).map((a) => digest(a, all, sla, mentionList));
  return {
    tickets: all,
    created: threaded.created,
    reopened: threaded.reopened,
    merged,
    automations,
    suggestions,
    released,
    assigned,
    sla,
    digests,
    tags: uniqueTags(all),
    topTags: tagCounts(all).slice(0, 10),
    waiting: waitingForAgent(all, now),
    csv: ticketsCsv(all, sla),
    report: weeklyReport(all, agents, slaCompliance(all, now - 7 * 24 * 3600 * 1000)),
    stats: agentStats(all, agents),
    flow: dailyFlow(all),
  };
}
