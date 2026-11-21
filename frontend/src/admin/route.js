// SPDX-License-Identifier: AGPL-3.0-only
// The admin's moves inside the page, pure (no DOM): what a click in the menu, a
// booking or a member pushes to the history (state and address) and the route it
// shows, and what a click leaves to the browser. `routeOf` (bookings.js) reads
// an address and its history state back, which is what popstate does.
import {
  IMPORTS_ADDRESS, PAYOUTS_ADDRESS, bookingAddress, importAddress, importsRoute, memberAddress, payoutAddress, payoutsRoute, placeRoute,
} from './bookings.js';
import { hrefOf } from './places.js';

// A plain left click is ours; a modifier key or another button is the browser's
// (a new tab, a new window).
export const plainClick = (event) =>
  !(event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey);

const onList = (route) => route.booking === null && route.member === null && !route.run && !route.payout;

// A place of the menu: its list. Null when that list is already shown.
export function placeStep(route, place) {
  // The Members' menu entry from its Imports tab goes to the members.
  if (route.place === place && onList(route) && !route.imports && !route.payouts) return null;
  return { state: {}, address: hrefOf(place), route: placeRoute(place) };
}

// A booking opened from a place, and a member opened from the Members: the entry
// says where from and that a list is behind it (`close` goes back to it).
export const bookingStep = (number, from) => ({
  state: { from, listed: true }, address: bookingAddress(number), route: { place: from, booking: number, member: null },
});
export const memberStep = (id) => ({
  state: { from: 'members', listed: true }, address: memberAddress(id), route: { place: 'members', booking: null, member: id },
});

// The Members place's Imports tab (its other tab is the members' own list,
// `placeStep`). Null when the tab is already shown.
export function importsStep(route) {
  if (route.place === 'members' && route.imports && !route.run) return null;
  return { state: {}, address: IMPORTS_ADDRESS, route: importsRoute() };
}

// A run opened from the imports; its page keeps the imports behind it.
export const runStep = (number) => ({
  state: { from: 'members', listed: true }, address: importAddress(number), route: importsRoute(number),
});

// The Payments place's Payouts tab (its other tab is the payments' own list,
// `placeStep`). Null when the tab is already shown.
export function payoutsStep(route) {
  if (route.place === 'payments' && route.payouts && !route.payout) return null;
  return { state: {}, address: PAYOUTS_ADDRESS, route: payoutsRoute() };
}

// A payout opened from the Payouts; its page keeps the payouts behind it.
export const payoutStep = (id) => ({
  state: { from: 'payments', listed: true }, address: payoutAddress(id), route: payoutsRoute(id),
});
