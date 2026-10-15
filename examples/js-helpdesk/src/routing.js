/**
 * Assignment of tickets to agents.
 *
 * An agent: { id, name, skills: [team or tag …], online, maxOpen }.
 * Open, unassigned tickets are distributed in the order priority (urgent first),
 * creation time, number. Candidates are agents who are online, have the team or
 * one of the ticket's tags as a skill and are below their maximum load. The one chosen is
 * the one with the fewest open tickets, on a tie the smaller ID (string comparison).
 */
import { isOpen, priorityRank } from './tickets.js';

/** Open tickets per agent ID. */
export function agentLoad(agents, tickets) {
  return agents.reduce(
    (acc, a) => ({ ...acc, [a.id]: tickets.filter((t) => t.assignee === a.id && isOpen(t)).length }),
    {},
  );
}

export function canHandle(agent, ticket) {
  if (!agent.online) return false;
  if (ticket.team !== null && agent.skills.includes(ticket.team)) return true;
  return ticket.tags.some((tag) => agent.skills.includes(tag));
}

export function assignmentOrder(tickets) {
  return tickets
    .filter((t) => isOpen(t) && t.assignee === null)
    .sort((a, b) => priorityRank(b.priority) - priorityRank(a.priority) || a.createdAt - b.createdAt || a.id - b.id);
}

/**
 * Distributes open, unassigned tickets.
 * @returns {Array<[number, string]>} (ticket number, agent ID) in assignment order
 */
export function autoAssign(tickets, agents, at) {
  const assigned = [];
  for (const ticket of assignmentOrder(tickets)) {
    const load = agentLoad(agents, tickets);
    const eligible = agents
      .filter((a) => canHandle(a, ticket) && load[a.id] < (a.maxOpen ?? Infinity))
      .sort((a, b) => load[a.id] - load[b.id] || (a.id < b.id ? -1 : a.id > b.id ? 1 : 0));
    if (eligible.length === 0) continue;
    ticket.assignee = eligible[0].id;
    ticket.updatedAt = at;
    assigned.push([ticket.id, eligible[0].id]);
  }
  return assigned;
}

/** Release the tickets of agents who went offline (only open ones, not pending ones). */
export function releaseOffline(tickets, agents, at) {
  const offline = new Set(agents.filter((a) => !a.online).map((a) => a.id));
  const released = [];
  for (const t of tickets) {
    if (t.assignee !== null && offline.has(t.assignee) && isOpen(t) && t.status !== 'pending') {
      t.assignee = null;
      t.updatedAt = at;
      released.push(t.id);
    }
  }
  return released;
}

/** Who is overloaded? Agents above their maximum load, largest overload first. */
export function overloaded(agents, tickets) {
  const load = agentLoad(agents, tickets);
  return agents
    .filter((a) => a.maxOpen !== undefined && load[a.id] > a.maxOpen)
    .map((a) => ({ id: a.id, load: load[a.id], over: load[a.id] - a.maxOpen }))
    .sort((a, b) => b.over - a.over || (a.id < b.id ? -1 : 1));
}

/** Skill coverage: which teams/tags does no online agent have? */
export function uncoveredSkills(tickets, agents) {
  const online = agents.filter((a) => a.online);
  const needed = new Set();
  for (const t of tickets) {
    if (!isOpen(t)) continue;
    if (t.team !== null) needed.add(t.team);
  }
  return [...needed].filter((skill) => !online.some((a) => a.skills.includes(skill))).sort();
}
