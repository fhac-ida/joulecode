/**
 * Key figures and exports for the team lead.
 */
import { csvCell } from './text.js';
import { isOpen } from './tickets.js';

const HOUR = 3600 * 1000;

/** Median (mean of the two middle values for an even count); does not modify the input. */
export function median(values) {
  if (values.length === 0) return null;
  const s = [...values].sort((a, b) => a - b);
  const mid = s.length >> 1;
  return s.length % 2 ? s[mid] : (s[mid - 1] + s[mid]) / 2;
}

/** Per agent: assigned, open, solved, median resolution time and first response in hours. */
export function agentStats(tickets, agents) {
  return agents
    .map((a) => {
      const mine = tickets.filter((t) => t.assignee === a.id);
      const solved = mine.filter((t) => t.resolvedAt !== null);
      const responded = mine.filter((t) => t.firstResponseAt !== null);
      return {
        id: a.id,
        name: a.name,
        assigned: mine.length,
        open: mine.filter(isOpen).length,
        solved: solved.length,
        medianResolutionH: round1(median(solved.map((t) => (t.resolvedAt - t.createdAt) / HOUR))),
        medianFirstResponseH: round1(median(responded.map((t) => (t.firstResponseAt - t.createdAt) / HOUR))),
      };
    })
    .sort((x, y) => y.solved - x.solved || (x.id < y.id ? -1 : 1));
}

function round1(x) {
  return x === null ? null : Math.round(x * 10) / 10;
}

/** Incoming and solved tickets per calendar day (UTC). */
export function dailyFlow(tickets) {
  const days = new Map();
  const bump = (t, key) => {
    const day = new Date(t).toISOString().slice(0, 10);
    const d = days.get(day) ?? { created: 0, solved: 0 };
    d[key]++;
    days.set(day, d);
  };
  for (const t of tickets) {
    bump(t.createdAt, 'created');
    if (t.resolvedAt !== null) bump(t.resolvedAt, 'solved');
  }
  return [...days.entries()].sort(([a], [b]) => (a < b ? -1 : 1)).map(([day, v]) => ({ day, ...v }));
}

export const CSV_COLUMNS = ['id', 'status', 'priority', 'assignee', 'team', 'created', 'resolved', 'sla', 'tags', 'subject'];

export function ticketsCsv(tickets, slaByTicket) {
  const rows = [CSV_COLUMNS.join(';')];
  for (const t of tickets) {
    rows.push(
      [
        t.id,
        t.status,
        t.priority,
        t.assignee ?? '',
        t.team ?? '',
        new Date(t.createdAt).toISOString(),
        t.resolvedAt === null ? '' : new Date(t.resolvedAt).toISOString(),
        slaByTicket.get(t.id)?.state ?? '',
        t.tags.join('|'),
        t.subject,
      ]
        .map(csvCell)
        .join(';'),
    );
  }
  return rows.join('\n') + '\n';
}

/** Weekly report as text. */
export function weeklyReport(tickets, agents, compliance, title = 'Helpdesk – weekly report') {
  const lines = [title, '='.repeat(title.length), ''];
  const open = tickets.filter(isOpen).length;
  lines.push(`Tickets: ${tickets.length}, open: ${open}, solved: ${tickets.filter((t) => t.resolvedAt !== null).length}`);
  lines.push('', 'SLA met:');
  for (const [prio, v] of Object.entries(compliance)) {
    lines.push(`  ${prio.padEnd(8)} ${(v.rate * 100).toFixed(1).padStart(6)} %  (${v.met}/${v.total})`);
  }
  lines.push('', 'Team:');
  for (const s of agentStats(tickets, agents).slice(0, 12)) {
    lines.push(`  ${s.name.padEnd(14)} solved ${String(s.solved).padStart(4)}  open ${String(s.open).padStart(3)}  avg resolution ${s.medianResolutionH ?? '–'} h`);
  }
  return lines.join('\n') + '\n';
}
