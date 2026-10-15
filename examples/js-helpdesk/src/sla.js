/**
 * Service level: deadlines in business hours.
 *
 * A calendar describes the business hours per weekday (minutes since midnight, UTC)
 * and holidays (YYYY-MM-DD). A minute [t, t+1 min) counts if its start lies within
 * business hours. `addBusinessMinutes(start, n)` returns the end of the n-th
 * business minute from `start` – if it ends exactly at closing time, the result is
 * the closing time of that day (not the next morning).
 */

export const MINUTE = 60 * 1000;

export const DEFAULT_CALENDAR = {
  // 0 = Sunday … 6 = Saturday: [start, end) in minutes since midnight
  hours: { 1: [480, 1080], 2: [480, 1080], 3: [480, 1080], 4: [480, 1080], 5: [480, 960] },
  holidays: ['2026-01-01', '2026-04-03', '2026-04-06', '2026-05-01', '2026-05-14', '2026-05-25', '2026-06-04', '2026-10-03', '2026-12-25', '2026-12-26'],
};

/** Targets per priority in business minutes. */
export const DEFAULT_POLICY = {
  urgent: { firstResponse: 15, resolution: 120 },
  high: { firstResponse: 60, resolution: 480 },
  normal: { firstResponse: 240, resolution: 960 },
  low: { firstResponse: 480, resolution: 1920 },
};

export function isoDate(t) {
  return new Date(t).toISOString().slice(0, 10);
}

export function isHoliday(t, calendar = DEFAULT_CALENDAR) {
  return calendar.holidays.includes(isoDate(t));
}

/** Does the start of the minute `t` lie within business hours? */
export function isBusinessMinute(t, calendar = DEFAULT_CALENDAR) {
  const d = new Date(t);
  const span = calendar.hours[d.getUTCDay()];
  if (!span) {
    return false;
  }
  const minuteOfDay = d.getUTCHours() * 60 + d.getUTCMinutes();
  if (minuteOfDay < span[0] || minuteOfDay >= span[1]) {
    return false;
  }
  return !isHoliday(t, calendar);
}

/** End of the n-th business minute from `start` (n = 0: `start`). */
export function addBusinessMinutes(start, minutes, calendar = DEFAULT_CALENDAR) {
  if (minutes < 0 || !Number.isInteger(minutes)) {
    throw new RangeError(`minutes must be a non-negative integer, not ${minutes}`);
  }
  let t = start;
  let left = minutes;
  while (left > 0) {
    if (isBusinessMinute(t, calendar)) {
      left--;
    }
    t += MINUTE;
  }
  return t;
}

/** Business minutes between a and b (minutes whose start lies in [a, b)). */
export function businessMinutesBetween(a, b, calendar = DEFAULT_CALENDAR) {
  if (b <= a) {
    return 0;
  }
  let count = 0;
  for (let t = a; t < b; t += MINUTE) {
    if (isBusinessMinute(t, calendar)) {
      count++;
    }
  }
  return count;
}

/** Next start of a business minute from `t` on (t itself if it already is business time). */
export function nextBusinessStart(t, calendar = DEFAULT_CALENDAR) {
  let x = t;
  for (let guard = 0; guard < 60 * 24 * 21; guard++) {
    if (isBusinessMinute(x, calendar)) {
      return x;
    }
    x += MINUTE;
  }
  throw new RangeError('calendar without business hours in the next three weeks');
}

/**
 * SLA state of a ticket.
 *
 * @returns {{ firstResponseDue, resolutionDue, firstResponseBreached, resolutionBreached,
 *             remainingMinutes, state: 'ok'|'warning'|'breached'|'met' }}
 */
export function slaStatus(ticket, now, policy = DEFAULT_POLICY, calendar = DEFAULT_CALENDAR) {
  const target = policy[ticket.priority];
  if (!target) {
    throw new RangeError(`no SLA for priority ${ticket.priority}`);
  }
  const firstResponseDue = addBusinessMinutes(ticket.createdAt, target.firstResponse, calendar);
  const resolutionDue = addBusinessMinutes(ticket.createdAt, target.resolution, calendar);
  const responded = ticket.firstResponseAt ?? null;
  const resolved = ticket.resolvedAt ?? null;
  const firstResponseBreached = responded === null ? now > firstResponseDue : responded > firstResponseDue;
  const resolutionBreached = resolved === null ? now > resolutionDue : resolved > resolutionDue;
  let state;
  let remainingMinutes = null;
  if (resolved !== null) {
    state = resolutionBreached ? 'breached' : 'met';
  } else if (firstResponseBreached || resolutionBreached) {
    state = 'breached';
  } else {
    remainingMinutes = businessMinutesBetween(now, resolutionDue, calendar);
    state = remainingMinutes <= target.resolution * 0.2 ? 'warning' : 'ok';
  }
  return { firstResponseDue, resolutionDue, firstResponseBreached, resolutionBreached, remainingMinutes, state };
}

/** Share of SLAs met per priority (tickets solved from `since` on). */
export function slaCompliance(tickets, since = -Infinity, policy = DEFAULT_POLICY, calendar = DEFAULT_CALENDAR) {
  const out = {};
  for (const t of tickets) {
    if (t.resolvedAt === null || t.resolvedAt < since) continue;
    const s = slaStatus(t, t.resolvedAt, policy, calendar);
    out[t.priority] ??= { met: 0, total: 0 };
    out[t.priority].total++;
    if (s.state === 'met') out[t.priority].met++;
  }
  return Object.fromEntries(
    Object.entries(out)
      .sort(([a], [b]) => a.localeCompare(b))
      .map(([p, v]) => [p, { ...v, rate: Math.round((v.met / v.total) * 1000) / 1000 }]),
  );
}

/** Tickets whose deadline expires within the next `minutes` business minutes, most urgent first. */
export function dueSoon(tickets, now, minutes = 60, policy = DEFAULT_POLICY, calendar = DEFAULT_CALENDAR) {
  const out = [];
  for (const t of tickets) {
    if (t.resolvedAt !== null || t.status === 'closed') continue;
    const s = slaStatus(t, now, policy, calendar);
    if (s.state !== 'breached' && s.remainingMinutes !== null && s.remainingMinutes <= minutes) {
      out.push({ id: t.id, remaining: s.remainingMinutes, due: s.resolutionDue });
    }
  }
  return out.sort((a, b) => a.remaining - b.remaining || a.id - b.id);
}
