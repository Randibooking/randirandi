// SPDX-License-Identifier: AGPL-3.0-only
// My page: the member's own bookings (part 3.8). Pure: no DOM, no fetch.
import { isNight, lineView, typeName } from './cart.js';
import { label } from './labels.js';

// An order's own text of the booking conditions, to its member .
export const conditionsHref = (order) => `/book/order/${encodeURIComponent(order)}/conditions/`;

// What one booking line shows: the type, then what an order's line shows
// (`lineView`, with its asset), its order's reference and id, and, for a cancelled
// booking, the word for it. Its price is in its own order's currency
// (`item.currency`); `currency` is for an item without one.
export function bookingView(item, currency) {
  const view = lineView(item, item.currency ?? currency, false, true);
  return {
    ...view,
    key: `${item.order.id}|${item.start}|${item.container?.id ?? ''}`,
    type: typeName(item),
    night: isNight(item),
    order: item.order.reference,
    orderWords: label('me.order', { reference: item.order.reference }),
    orderId: item.order.id,
    // The booking conditions its order accepted : the address of that text,
    // or null for a legacy order without recorded conditions.
    conditionsHref: item.order.conditions ? conditionsHref(item.order.id) : null,
    day: item.start.slice(0, 10),
    start: item.start,
    cancelled: item.state === 'cancelled' ? label('line.cancelled') : null,
  };
}

// The two lists as the page shows them; each is empty when the server sent none.
// A line's key carries its place in its list: one order can hold two bookings
// of the same container at the same start.
export function mineView(data, currency) {
  const lines = (items, list) => (items || []).map((item, index) => {
    const view = bookingView(item, currency);
    return { ...view, key: `${list}|${index}|${view.key}` };
  });
  const coming = lines(data?.coming, 'coming');
  const earlier = lines(data?.earlier, 'earlier');
  return {
    coming, earlier, nothingComing: coming.length === 0,
    comingDays: dayGroups(coming), earlierDays: dayGroups(earlier),
  };
}

// The lines under their date, each date once, in the order the dates first
// come in the list; inside a date the lines in time order.
export function dayGroups(lines) {
  const found = new Map();
  for (const line of lines) {
    if (!found.has(line.day)) found.set(line.day, { day: line.day, date: line.date, lines: [] });
    found.get(line.day).lines.push(line);
  }
  return [...found.values()].map((group) => ({
    ...group,
    lines: [...group.lines].sort((a, b) => Date.parse(a.start) - Date.parse(b.start)),
  }));
}

const joined = (words) => words.reduce((list, next) => label('list.join', { list, next }));

// The email that asks the organization to cancel one booking, as a `mailto:`
// address; null when the organization has no contact address, or when the
// booking is cancelled (a booking still to come is the caller's to give).
export function cancelMail(line, contact, name) {
  if (!contact || line.cancelled) return null;
  const what = joined([line.type, line.asset, line.date, line.time].filter(Boolean));
  const subject = label('me.cancel-subject', { reference: line.order });
  const body = label('me.cancel-body', { reference: line.order, what, name: name || '' }).trim();
  // The address is encoded too: an email's local part may hold ? & # or %.
  const to = encodeURIComponent(contact).replace(/%40/g, '@');
  return `mailto:${to}?subject=${encodeURIComponent(subject)}&body=${encodeURIComponent(body)}`;
}

// Who the member is, as rows of label and values; a value that is missing is
// left out, and so is its row. The name is the heading, not a row.
export function whoRows(config) {
  const memberships = config?.member_memberships || [];
  const rows = [
    ['membership', 'me.membership', memberships.map((one) => one.identifier)],
    ['email', 'me.email', [config?.member_email]],
    ['phone', 'me.phone', [config?.member_phone]],
    ['address', 'me.address', memberships.map((one) => one.address)],
    ['role', 'me.role', [config?.member_role]],
  ];
  return rows
    .map(([key, words, values]) => ({ key, label: label(words), values: values.filter(Boolean) }))
    .filter((row) => row.values.length > 0);
}

// The result of one read of the member's bookings as the page needs it.
export function readMine(result) {
  if (result.ok) return { kind: 'ready', data: result.data };
  return { kind: result.kind };
}

// "Alex Rivers" -> "AR"; at most two letters.
export const initialsOf = (name) => (name || '').split(/\s+/).filter(Boolean).slice(0, 2)
  .map((word) => [...word][0].toUpperCase()).join('');

// The line under who the member is: the booking categories their role has, in
// the organization's order ("Books as Private, Work"); null until the choices
// are in or when there are none.
export function booksAs(choices) {
  const names = (choices?.categories || []).map((row) => row.name).filter(Boolean);
  return names.length ? label('me.books-as', { categories: names.reduce((list, next) => label('list.join', { list, next })) }) : null;
}

// The rows at the end of My page: Admin (administrators only, a link to the
// admin) and Sign out (the shell's POST form). The only place in the member
// pages that leads to either.
export const accountRows = (admin) => (admin
  ? [{ key: 'admin', href: '/admin/' }, { key: 'sign-out' }]
  : [{ key: 'sign-out' }]);
