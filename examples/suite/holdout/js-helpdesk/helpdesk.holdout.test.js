// Holdout tests for js-helpdesk – joule:code does not see them.
import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
  addBusinessMinutes,
  agentLoad,
  autoAssign,
  businessMinutesBetween,
  createTicket,
  DEFAULT_CALENDAR,
  slaStatus,
  uniqueTags,
} from '../src/index.js';

const at = (m, d, h, min = 0, s = 0) => Date.UTC(2026, m - 1, d, h, min, s);

test('deadline ends exactly at closing time: the same day, not the next morning', () => {
  assert.equal(addBusinessMinutes(at(5, 4, 17, 30), 30), at(5, 4, 18));
  assert.equal(addBusinessMinutes(at(5, 4, 8), 600), at(5, 4, 18));
});

test('Friday closes earlier, the weekend does not count', () => {
  assert.equal(addBusinessMinutes(at(5, 8, 15, 50), 20), at(5, 11, 8, 10));
  assert.equal(addBusinessMinutes(at(5, 8, 15, 50), 10), at(5, 8, 16));
});

test('holiday (Whit Monday, 25 May) is skipped', () => {
  assert.equal(addBusinessMinutes(at(5, 22, 15, 59), 2), at(5, 26, 8, 1));
  assert.equal(businessMinutesBetween(at(5, 22, 15), at(5, 26, 9)), 120);
});

test('start with seconds: the offset is preserved', () => {
  assert.equal(addBusinessMinutes(at(5, 4, 7, 59, 30), 1), at(5, 4, 8, 1, 30));
  assert.equal(addBusinessMinutes(at(5, 4, 17, 59, 30), 1), at(5, 4, 18, 0, 30));
  assert.equal(businessMinutesBetween(at(5, 4, 7, 59, 30), at(5, 4, 8, 2)), 2);
});

test('zero minutes: start time, even outside business hours', () => {
  assert.equal(addBusinessMinutes(at(5, 10, 3), 0), at(5, 10, 3));
  assert.equal(businessMinutesBetween(at(5, 4, 12), at(5, 4, 12)), 0);
  assert.equal(businessMinutesBetween(at(5, 4, 12), at(5, 4, 11)), 0);
});

test('long deadlines across a month boundary and several holidays', () => {
  const t = at(5, 13, 12);
  const end = addBusinessMinutes(t, 1920);
  // Wed 6 h, Thu 14 May holiday, Fri 8 h (until 4 pm), Mon 10 h, Tue 8 h → Tue 19 May, 16:00
  assert.equal(end, at(5, 19, 16));
  assert.equal(businessMinutesBetween(t, end), 1920);
});

test('custom calendar without holidays', () => {
  const cal = { hours: { 0: [0, 1440], 6: [0, 1440] }, holidays: [] };
  assert.equal(addBusinessMinutes(at(5, 8, 23), 120, cal), at(5, 9, 2));
  assert.equal(DEFAULT_CALENDAR.holidays.includes('2026-05-25'), true);
});

test('SLA with a response exactly at the deadline is met', () => {
  const t = createTicket({ id: 1, subject: 'x', requester: 'a@b.de', createdAt: at(5, 4, 9), priority: 'urgent' });
  t.firstResponseAt = at(5, 4, 9, 15);
  t.resolvedAt = at(5, 4, 11);
  const s = slaStatus(t, at(5, 5, 9));
  assert.equal(s.firstResponseBreached, false);
  assert.equal(s.state, 'met');
});

const mk = (id, extra = {}) => {
  const t = createTicket({ id, subject: `T${id}`, requester: 'a@b.de', createdAt: at(5, 4, 9) + id, team: 'hw', ...extra });
  return t;
};

test('tie in the assignment: string comparison of the IDs ("10" before "9")', () => {
  const agents = [
    { id: '9', name: 'Nine', skills: ['hw'], online: true },
    { id: '10', name: 'Ten', skills: ['hw'], online: true },
  ];
  const ts = [mk(1), mk(2), mk(3)];
  assert.deepEqual(autoAssign(ts, agents, at(5, 4, 10)), [[1, '10'], [2, '9'], [3, '10']]);
});

test('maximum load counts already assigned open tickets, not solved ones', () => {
  const agents = [
    { id: 'x', name: 'X', skills: ['hw'], online: true, maxOpen: 2 },
    { id: 'y', name: 'Y', skills: ['hw'], online: true, maxOpen: 1 },
  ];
  const done = mk(10, { assignee: 'x' });
  done.status = 'solved';
  const busy = mk(11, { assignee: 'y' });
  busy.status = 'open';
  const ts = [done, busy, mk(1), mk(2), mk(3)];
  assert.deepEqual(autoAssign(ts, agents, at(5, 4, 10)), [[1, 'x'], [2, 'x']]);
  assert.equal(ts[4].assignee, null);
  assert.deepEqual(agentLoad(agents, ts), { x: 2, y: 1 });
});

test('urgent tickets first, then the oldest', () => {
  const agents = [{ id: 'a', name: 'A', skills: ['hw'], online: true, maxOpen: 1 }];
  const ts = [mk(1), mk(2, { priority: 'urgent' })];
  assert.deepEqual(autoAssign(ts, agents, at(5, 4, 10)), [[2, 'a']]);
});

test('tags: case-sensitive', () => {
  assert.deepEqual(uniqueTags([mk(1, { tags: ['VPN', 'vpn'] }), mk(2, { tags: ['vpn'] })]), ['VPN', 'vpn']);
});
