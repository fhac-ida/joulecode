/**
 * Benchmark: fixed, deterministic amount of work (one working day in the helpdesk).
 *
 * BENCH_ROUNDS scales the run time (on the measurement machine, choose it so that a run takes >= 20 s).
 * BENCH_TICKETS / BENCH_MAILS change the amount of data per round.
 */
import { createTicket, processDay } from '../src/index.js';

const ROUNDS = Number(process.env.BENCH_ROUNDS ?? 3);
const TICKETS = Number(process.env.BENCH_TICKETS ?? 1500);
const MAILS = Number(process.env.BENCH_MAILS ?? 800);

// Fixed random generator (mulberry32) so that every variant gets the same data.
let seed = 1234;
const random = () => {
  seed = (seed + 0x6d2b79f5) | 0;
  let t = Math.imul(seed ^ (seed >>> 15), 1 | seed);
  t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
  return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
};
const pick = (xs) => xs[Math.floor(random() * xs.length)];

const MIN = 60 * 1000;
const DAY0 = Date.UTC(2026, 4, 4, 0, 0); // Monday
const TOPICS = ['Printer not printing', 'VPN keeps dropping', 'Forgot password', 'Invoice charged twice', 'Laptop does not start', 'Licence expired', 'Wifi slow', 'Login locked', 'Monitor flickering', 'Install software'];
const TEAMS = ['hardware', 'network', 'access', 'billing', 'software'];
const BODIES = ['It has not worked since this morning.', 'Please help urgently, outage in the whole office!', 'No error visible, but very slow.', 'It still worked yesterday.', 'See attachment, thanks.'];

function data() {
  seed = 1234;
  const agents = Array.from({ length: 36 }, (_, i) => ({
    id: i < 30 ? `a${String(i).padStart(2, '0')}` : String(i - 22), // some purely numeric IDs
    name: `Agent${i}`,
    skills: [TEAMS[i % 5], TEAMS[(i + 2) % 5], ...(i % 4 === 0 ? ['vip'] : [])],
    online: i % 9 !== 8,
    maxOpen: 60 + (i % 5) * 10,
  }));
  const requesters = Array.from({ length: 700 }, (_, i) => `user${i}@example.com`);
  const tickets = [];
  for (let id = 1; id <= TICKETS; id++) {
    const createdAt = DAY0 - Math.floor(random() * 20 * 24 * 60) * MIN;
    const t = createTicket({
      id,
      subject: `${pick(TOPICS)} ${id % 50}`,
      requester: pick(requesters),
      createdAt,
      priority: pick(['low', 'normal', 'normal', 'normal', 'high', 'urgent']),
      tags: [pick(TEAMS), ...(random() < 0.1 ? ['vip'] : [])],
      team: pick(TEAMS),
      messages: [{ from: 'x', at: createdAt, body: pick(BODIES), internal: false }],
    });
    t.messages[0].from = t.requester;
    const r = random();
    if (r < 0.55) {
      t.status = 'solved';
      t.assignee = pick(agents).id;
      t.firstResponseAt = createdAt + Math.floor(random() * 600) * MIN;
      t.resolvedAt = t.firstResponseAt + Math.floor(random() * 3000) * MIN;
      t.updatedAt = t.resolvedAt;
    } else if (r < 0.75) {
      t.status = 'open';
      t.assignee = random() < 0.7 ? pick(agents).id : null;
      t.firstResponseAt = createdAt + Math.floor(random() * 600) * MIN;
      t.updatedAt = t.firstResponseAt;
      t.messages.push({ from: t.assignee ?? 'bot', at: t.updatedAt, body: `We are looking into it. @Agent${Math.floor(random() * 36)}`, internal: random() < 0.5 });
    } else if (r < 0.85) {
      t.status = 'pending';
      t.assignee = pick(agents).id;
    } else if (r < 0.9) {
      t.status = 'closed';
      t.resolvedAt = createdAt + 3 * 24 * 60 * MIN;
      t.updatedAt = t.resolvedAt;
    }
    tickets.push(t);
  }
  const mails = [];
  for (let i = 0; i < MAILS; i++) {
    const at = DAY0 + (6 * 60 + Math.floor(random() * 14 * 60)) * MIN;
    const r = random();
    if (r < 0.35) {
      const t = pick(tickets);
      mails.push({ at, from: t.requester, subject: `AW: [#${t.id}] ${t.subject}`, body: pick(BODIES) });
    } else if (r < 0.6) {
      const t = pick(tickets);
      mails.push({ at, from: t.requester.toUpperCase(), subject: `Re: ${t.subject}`, body: pick(BODIES) });
    } else {
      mails.push({ at, from: pick(requesters), subject: `${pick(TOPICS)} ${i % 80}`, body: pick(BODIES) });
    }
  }
  const rules = [
    { id: 'vip', when: { tag: 'vip', priority: ['low', 'normal'] }, then: [{ action: 'setPriority', value: 'high' }] },
    { id: 'outage', when: { subjectMatches: 'outage|down' }, then: [{ action: 'setPriority', value: 'urgent' }], stop: true },
    { id: 'invoice', when: { subjectMatches: 'invoice' }, then: [{ action: 'setTeam', value: 'billing' }] },
    { id: 'remind', when: { status: 'pending', olderThanHours: 72 }, then: [{ action: 'reply', value: 'Hello {{requester}}, is there any news on #{{id}}?' }] },
  ];
  const articles = TOPICS.map((topic, i) => ({ id: `kb${i}`, title: topic, body: `${topic}: solution steps. ${BODIES[i % BODIES.length]}`, tags: [TEAMS[i % 5]] }));
  return { agents, tickets, mails, rules, articles };
}

const NOW = DAY0 + 21 * 60 * MIN;
const start = process.hrtime.bigint();
let checksum = 0;
for (let r = 0; r < ROUNDS; r++) {
  const { agents, tickets, mails, rules, articles } = data(); // fresh data every round (processDay modifies it)
  const out = processDay(tickets, agents, mails, NOW, { rules, articles });
  checksum += out.created.length * 7 + out.reopened.length * 5 + out.merged.length * 3 + out.assigned.length;
  checksum += out.tags.length + out.csv.length + out.report.length + out.waiting.length + out.automations.length;
  for (const s of out.sla.values()) checksum += s.state.length + (s.remainingMinutes ?? 0);
  for (const d of out.digests) checksum += d.length;
}
const seconds = Number(process.hrtime.bigint() - start) / 1e9;
console.log(`rounds=${ROUNDS} checksum=${checksum} seconds=${seconds.toFixed(2)}`);
