import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
  addBusinessMinutes,
  agentLoad,
  autoAssign,
  businessMinutesBetween,
  createTicket,
  isBusinessMinute,
  median,
  mergeDuplicates,
  normalizeSubject,
  searchTickets,
  slaStatus,
  threadMessages,
  transition,
  uniqueTags,
} from '../src/index.js';
import { normalizeSubject as norm } from '../src/text.js';

const MIN = 60 * 1000;
const at = (d, h, m = 0) => Date.UTC(2026, 4, d, h, m); // May 2026; the 4th is a Monday

const ticket = (id, extra = {}) =>
  createTicket({ id, subject: `Problem ${id}`, requester: 'customer@example.com', createdAt: at(4, 9), ...extra });

test('subject without reply prefixes', () => {
  assert.equal(norm('AW: Re: Fwd: Printer  broken'), 'Printer broken');
  assert.equal(normalizeSubject('WG: Test'), 'Test');
});

test('business minutes within one day', () => {
  assert.equal(isBusinessMinute(at(4, 8)), true);
  assert.equal(isBusinessMinute(at(4, 18)), false);
  assert.equal(addBusinessMinutes(at(4, 9), 90), at(4, 10, 30));
});

test('business minutes overnight', () => {
  assert.equal(addBusinessMinutes(at(4, 17), 120), at(5, 9));
  assert.equal(businessMinutesBetween(at(4, 17), at(5, 9)), 120);
});

test('SLA state of an open ticket', () => {
  const t = ticket(1, { priority: 'high' });
  const s = slaStatus(t, at(4, 9, 30));
  assert.equal(s.firstResponseDue, at(4, 10));
  assert.equal(s.resolutionDue, at(4, 17));
  assert.equal(s.state, 'ok');
  assert.equal(slaStatus(t, at(4, 10, 1)).state, 'breached');
});

test('status change', () => {
  const t = ticket(1);
  transition(t, 'open', at(4, 10));
  transition(t, 'solved', at(4, 11));
  assert.equal(t.resolvedAt, at(4, 11));
  assert.throws(() => transition(t, 'pending', at(4, 12)));
});

test('sorting in mails: reference, same subject, new', () => {
  const tickets = [ticket(1, { subject: 'Printer broken' })];
  const { tickets: all, created } = threadMessages(tickets, [
    { at: at(4, 10), from: 'customer@example.com', subject: 'Re: Printer broken', body: 'still there' },
    { at: at(4, 11), from: 'other@example.com', subject: 'AW: [#1] whatever', body: 'me too' },
    { at: at(4, 12), from: 'new@example.com', subject: 'VPN not working', body: 'urgent' },
  ]);
  assert.deepEqual(created, [2]);
  assert.equal(all[0].messages.length, 2);
  assert.equal(all[1].priority, 'high');
  assert.deepEqual(all[1].tags, ['network']);
});

test('merging duplicates', () => {
  const a = ticket(1, { subject: 'Login', createdAt: at(4, 9) });
  const b = ticket(2, { subject: 'AW: Login', createdAt: at(4, 9, 30), tags: ['access'] });
  assert.deepEqual(mergeDuplicates([b, a]), [[1, 2]]);
  assert.equal(b.status, 'closed');
  assert.deepEqual(a.tags, ['access']);
});

test('tags in order of first occurrence', () => {
  assert.deepEqual(uniqueTags([ticket(1, { tags: ['b', 'a'] }), ticket(2, { tags: ['a', 'c'] })]), ['b', 'a', 'c']);
});

test('search by text and priority', () => {
  const ts = [ticket(1, { subject: 'Printer not printing' }), ticket(2, { subject: 'VPN', priority: 'urgent' })];
  assert.deepEqual(searchTickets(ts, { text: 'printer' }).items.map((t) => t.id), [1]);
  assert.deepEqual(searchTickets(ts, { sort: 'priority' }).items.map((t) => t.id), [2, 1]);
});

test('assignment by load', () => {
  const agents = [
    { id: 'b', name: 'B', skills: ['hardware'], online: true },
    { id: 'a', name: 'A', skills: ['hardware'], online: true },
  ];
  const ts = [ticket(1, { team: 'hardware' }), ticket(2, { team: 'hardware' }), ticket(3, { team: 'hardware', assignee: 'b' })];
  ts[2].status = 'open';
  assert.deepEqual(autoAssign(ts, agents, at(4, 10)), [[1, 'a'], [2, 'a']]);
  assert.deepEqual(agentLoad(agents, ts), { b: 1, a: 2 });
});

test('Median', () => {
  assert.equal(median([3, 1, 2]), 2);
  assert.equal(median([4, 1, 2, 3]), 2.5);
  assert.equal(median([]), null);
});
