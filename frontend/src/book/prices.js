// SPDX-License-Identifier: AGPL-3.0-only
// The prices page's content, from the calendar's own answers (): each
// container's price tiers per category (the price book), its weekdays and
// hours, and worked examples. Pure: no DOM, no fetch.
import { lengthWords, moneyWords } from './add.js';
import { partTotal, tierParts } from './day.js';
import { label } from './labels.js';
import { clockOf } from './stack.js';
import { hhmm, WEEKDAY_CODES, weekdayIndex } from './words.js';

const wall = (moment) => Number(moment.slice(11, 13)) * 60 + Number(moment.slice(14, 16));
const dayName = (index) => label(`weekdays.${WEEKDAY_CODES[index]}`);

// "Monday to Friday", "Saturday and Sunday", "Friday", "Every day": the
// weekdays as runs, the week a circle (Sunday to Thursday nights).
export function daysWords(indices) {
  const days = new Set(indices);
  if (days.size === 7) return label('prices.every-day');
  const runs = [];
  for (let day = 0; day < 7; day += 1) {
    if (!days.has(day) || days.has((day + 6) % 7)) continue;
    let end = day;
    while (days.has((end + 1) % 7)) end = (end + 1) % 7;
    runs.push([day, end]);
  }
  return runs.map(([first, last]) => {
    const count = ((last - first + 7) % 7) + 1;
    if (count === 1) return dayName(first);
    return label(count === 2 ? 'prices.days-pair' : 'prices.days-range', { first: dayName(first), last: dayName(last) });
  }).join(', ');
}

// The tiers in words: "kr 250 for the first 4 h", "Then kr 100 per started
// 4 h", "At most 12 h"; by time of day "Until 17:00: kr 500 per started 4 h".
function ruleLines(row, container, night, currency) {
  const money = (amount) => moneyWords(amount, currency);
  const { tiers } = row;
  if (tiers.every((tier) => tier.amount === 0)) return [label('prices.free')];
  if (container.period_type === 'fixed') return [label(night ? 'prices.night' : 'prices.booking', { amount: money(tiers[0].amount) })];
  if (row.tiers_go_by === 'length') {
    const lines = tiers.map((tier, index) => {
      if (index === 0 && tier.paid_per >= tier.up_to) return label('prices.first', { amount: money(tier.amount), length: lengthWords(tier.up_to) });
      const values = { amount: money(tier.amount), length: lengthWords(tier.paid_per) };
      return label(index === 0 ? 'prices.per-started' : 'prices.then', values);
    });
    if (container.maximum_length) lines.push(label('prices.most', { length: lengthWords(container.maximum_length) }));
    return lines;
  }
  let previous = null;
  return tiers.map((tier) => {
    const from = previous;
    previous = tier.up_to;
    return label(from === null ? 'prices.until' : 'prices.clock', {
      from: clockOf(from ?? 0), to: clockOf(tier.up_to), amount: money(tier.amount), length: lengthWords(tier.paid_per),
    });
  });
}

// A category's own hours where it has them (Work: to 17:00 on a Friday), by weekday.
function ownHours(container, category) {
  const byWords = new Map();
  for (const period of container.periods) {
    const own = period.category_hours.find((item) => Number(item.category) === Number(category));
    if (!own) continue;
    const words = own.spans.map(([first, last]) => `${hhmm(first)}–${hhmm(last)}`).join(', ');
    if (!byWords.has(words)) byWords.set(words, new Set());
    byWords.get(words).add(weekdayIndex(period.day));
  }
  return [...byWords].map(([words, days]) => ({ first: Math.min(...days), line: label('prices.day-hours', { days: daysWords([...days]), hours: words }) }))
    .sort((a, b) => a.first - b.first).map((one) => one.line);
}

// The spans a category's tiers show best: the first tier's length and one
// interval more (by length), or up to and across the clock's change (by time
// of day), from the start of the hours.
function spansOf(container, row, start) {
  if (container.period_type === 'fixed' || row.groups.length || row.tiers.every((tier) => tier.amount === 0)) return [];
  const boundary = row.tiers[0].up_to;
  return row.tiers_go_by === 'length'
    ? [[start, start + boundary], [start, start + boundary + container.interval]]
    : [[start, Math.min(start + 4 * 60, boundary)], [boundary - 2 * 60, boundary + 2 * 60]];
}

// One span's price for one category: the money, "Free", or null where the
// category cannot book it (not offered, too short or too long, no tier).
function spanTotal(container, row, [from, to], currency) {
  if (!row) return null;
  if (to - from < container.minimum_length || to - from > container.maximum_length) return null;
  if (row.tiers.every((tier) => tier.amount === 0)) return label('price.free');
  const parts = tierParts(row.tiers, row.tiers_go_by, from, to);
  if (!parts) return null;
  const total = parts.reduce((sum, part) => sum + partTotal(part), 0);
  return total ? moneyWords(total, currency) : label('price.free');
}

// The columns of the matrix: the standard role's categories as the server
// sent them (), whatever the signed-in member's own role holds.
export const priceColumns = (configured) => (configured || []).map((row) => ({ id: row.id, name: row.name, icon: row.icon }));

// One column's question to the calendar: the standard role's answer (`prices:
// 1`, ), not the signed-in member's own, for the category of the column.
export const priceQuery = (query, category, start, days) => ({ ...query, category, start, days, prices: 1 });

// The page as a matrix ("design the price list as matrix. Privat and Work
// two cols."): one section per booking type in the organization's order, one
// row per container (its weekdays and hours) with a cell per category, then
// worked examples priced in every column. `answers`: the calendar's answers,
// one per category (a container that does not take a category is not in its
// answer).
export function priceBook(answers, categories, order) {
  const byId = new Map();
  for (const answer of answers) {
    for (const container of answer.containers) {
      const was = byId.get(container.id);
      if (!was) byId.set(container.id, { ...container, periods: [...container.periods] });
      else for (const period of container.periods) if (!was.periods.some((one) => one.day === period.day)) was.periods.push(period);
    }
  }
  const types = new Map();
  for (const container of [...byId.values()].sort((a, b) => a.number - b.number)) {
    if (!container.periods.length) continue;
    const longest = container.periods.reduce((best, one) => (one.rows.length > best.rows.length ? one : best));
    const night = longest.end.slice(0, 10) > longest.day;
    const days = daysWords([...new Set(container.periods.map((one) => weekdayIndex(one.day)))]);
    const takes = new Set((container.categories || []).map(Number));
    const currency = container.prices?.currency;
    const tiersOf = (category) => container.prices?.categories.find((one) => Number(one.category) === Number(category.id));
    const offered = (category) => takes.has(Number(category.id)) && (!container.with_payment || Boolean(tiersOf(category)));
    const cells = categories.map((category) => {
      if (!offered(category)) return { category, offered: false };
      const lines = container.with_payment ? ruleLines(tiersOf(category), container, night, currency) : [label('prices.free')];
      return { category, offered: true, lines, hours: ownHours(container, category.id) };
    });
    // The examples: every category's telling spans, at most three, each priced
    // in every column.
    const start = wall(longest.rows[0]);
    const spans = new Map();
    if (container.with_payment) {
      for (const category of categories.filter(offered)) {
        for (const span of spansOf(container, tiersOf(category), start)) spans.set(span.join('-'), span);
      }
    }
    const examples = [...spans.values()].sort((a, b) => a[0] - b[0] || a[1] - b[1]).slice(0, 3).map(([from, to]) => ({
      hours: `${clockOf(from)}–${clockOf(to)}`, length: lengthWords(to - from),
      totals: categories.map((category) => (offered(category) ? spanTotal(container, tiersOf(category), [from, to], currency) : null)),
    })).filter((example) => example.totals.some(Boolean));
    if (!types.has(container.type.name)) types.set(container.type.name, { name: container.type.name, night, blocks: [] });
    types.get(container.type.name).blocks.push({
      id: container.id, days: night ? label('prices.nights', { days }) : days, hours: `${hhmm(longest.rows[0])}–${hhmm(longest.end)}`, cells, examples,
    });
  }
  const rank = (name) => (order.includes(name) ? order.indexOf(name) : order.length);
  return [...types.values()].sort((a, b) => rank(a.name) - rank(b.name));
}
