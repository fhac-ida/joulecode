/**
 * Automation rules (“triggers”) and text templates.
 *
 * A rule: { id, when: { status?, priority?, tag?, team?, subjectMatches?, olderThanHours? },
 *               then: [{ action: 'setPriority'|'addTag'|'setTeam'|'setStatus'|'reply', value }], stop? }
 * Rules run in the order of the list; `stop` ends the processing of the ticket.
 * Every rule fires at most once per ticket (remembered in the tag `auto:<id>`).
 */
import { addAgentReply, transition, TicketError } from './tickets.js';

const HOUR = 3600 * 1000;

export function matches(rule, ticket, now) {
  const w = rule.when ?? {};
  if (w.status && ![w.status].flat().includes(ticket.status)) return false;
  if (w.priority && ![w.priority].flat().includes(ticket.priority)) return false;
  if (w.tag && !ticket.tags.includes(w.tag)) return false;
  if (w.team !== undefined && ticket.team !== w.team) return false;
  if (w.subjectMatches && !new RegExp(w.subjectMatches, 'i').test(ticket.subject)) return false;
  if (w.olderThanHours !== undefined && now - ticket.updatedAt < w.olderThanHours * HOUR) return false;
  return true;
}

/** Text template with placeholders {{field}}; unknown placeholders are left as they are. */
export function renderTemplate(template, ticket, extra = {}) {
  const values = { id: ticket.id, subject: ticket.subject, requester: ticket.requester, ...extra };
  return template.replace(/\{\{\s*(\w+)\s*\}\}/g, (all, key) => (key in values ? String(values[key]) : all));
}

/**
 * Apply rules to all tickets.
 * @returns {Array<{ ticket, rule }>} triggered rules in order
 */
export function runAutomations(tickets, rules, now, botName = 'helpdesk-bot') {
  const fired = [];
  for (const t of tickets) {
    for (const rule of rules) {
      const marker = `auto:${rule.id}`;
      if (t.tags.includes(marker) || !matches(rule, t, now)) continue;
      for (const step of rule.then) {
        apply(step, t, now, botName);
      }
      t.tags.push(marker);
      fired.push({ ticket: t.id, rule: rule.id });
      if (rule.stop) break;
    }
  }
  return fired;
}

function apply(step, ticket, now, botName) {
  switch (step.action) {
    case 'setPriority':
      ticket.priority = step.value;
      break;
    case 'addTag':
      if (!ticket.tags.includes(step.value)) ticket.tags.push(step.value);
      break;
    case 'setTeam':
      ticket.team = step.value;
      break;
    case 'setStatus':
      transition(ticket, step.value, now);
      break;
    case 'reply':
      addAgentReply(ticket, botName, now, renderTemplate(step.value, ticket));
      break;
    default:
      throw new TicketError(`unknown action ${step.action}`);
  }
  ticket.updatedAt = now;
}

/** Check rules in advance (unknown actions, invalid expressions, duplicate IDs). */
export function validateRules(rules) {
  const problems = [];
  const ids = new Set();
  for (const r of rules) {
    if (ids.has(r.id)) problems.push(`rule ${r.id}: duplicate ID`);
    ids.add(r.id);
    if (!Array.isArray(r.then) || r.then.length === 0) problems.push(`rule ${r.id}: no actions`);
    for (const s of r.then ?? []) {
      if (!['setPriority', 'addTag', 'setTeam', 'setStatus', 'reply'].includes(s.action)) {
        problems.push(`rule ${r.id}: unknown action ${s.action}`);
      }
    }
    if (r.when?.subjectMatches) {
      try {
        new RegExp(r.when.subjectMatches);
      } catch {
        problems.push(`rule ${r.id}: invalid expression ${r.when.subjectMatches}`);
      }
    }
  }
  return problems;
}
