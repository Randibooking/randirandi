// SPDX-License-Identifier: AGPL-3.0-only
// The administrators' calls to the server (randirandi/core/admin_views.py). The
// answers are those of book/api.js: {ok, data}, signed-out, forbidden, refused, offline.
import { call } from '../book/api.js';

// query: from, to, by, action, q, order, page (see audit.js: auditQuery)
export const readAuditEvents = (query) => call('/admin/audit/events/', { query });

// query: from, to, state, q, sort, order, page (see bookings.js: bookingsQuery)
export const readBookings = (query) => call('/admin/bookings/list/', { query });
// query: from, to, state, q, order, page (see bookings.js: paymentsQuery)
export const readPayments = (query) => call('/admin/payments/list/', { query });
// query: when, from, to, q, order, page (see bookings.js: scheduleQuery)
export const readSchedule = (query) => call('/admin/schedule/list/', { query });
// One booking, by its number. Reading it is looking it up: the server writes
// one "inspected" event.
export const readBooking = (number) => call(`/admin/bookings/${encodeURIComponent(number)}/data/`);

// Cancel a booking, or one of its lines, with or without the refund (part
// 7.7). body: {key, line, refund} (see bookings.js: cancelBody); `key` is new
// for each time the dialog opens, so a double press cancels once. The answer is
// the booking's page after the cancel.
export const cancelBooking = (csrf, number, body) =>
  call(`/admin/bookings/${encodeURIComponent(number)}/cancel/`, { method: 'POST', csrf, body });

// The Blocks (part 7.9). query: when, from, to, order, page (see blockForm.js:
// blocksQuery). The answer also lists the assets a new block can be on.
export const readBlocks = (query) => call('/admin/blocks/list/', { query });
// What a stretch would be laid as; query: asset, start, end (on the asset's clock,
// "2027-03-01T10:00"). Written nowhere.
export const layBlocks = (query) => call('/admin/blocks/lay/', { query });
// Make the blocks of a stretch, all or none. body: {asset, start, end, note}
// (see blockForm.js: makeBody).
export const makeBlocks = (csrf, body) => call('/admin/blocks/make/', { method: 'POST', csrf, body });
// Remove a block that has not ended; its time is free at once.
export const removeBlock = (csrf, id) =>
  call(`/admin/blocks/${encodeURIComponent(id)}/remove/`, { method: 'POST', csrf });

// The Members (part 7.91). query: status, role, q, sort, order, page (see
// members.js: membersQuery). 25 members a page; there is no call for all of them.
export const readMembers = (query) => call('/admin/members/list/', { query });
// One member, by id. Reading it is looking it up: the server writes one
// "inspected" event.
export const readMember = (id) => call(`/admin/members/${encodeURIComponent(id)}/data/`);
// Block or unblock a member, with no body. The answer is the member's page after
// the change; a second press is refused, not repeated.
export const blockMember = (csrf, id) => call(`/admin/members/${encodeURIComponent(id)}/block/`, { method: 'POST', csrf });
export const unblockMember = (csrf, id) => call(`/admin/members/${encodeURIComponent(id)}/unblock/`, { method: 'POST', csrf });
// Correct a member, change their email, export their data (part 7.92); each
// asks for the administrator's authenticator code in the body (see members.js:
// correctBody, emailBody, codeBody). The first two answer the member's page
// after the change; the export answers the member's data (a download).
export const correctMember = (csrf, id, body) =>
  call(`/admin/members/${encodeURIComponent(id)}/correct/`, { method: 'POST', csrf, body });
export const changeMemberEmail = (csrf, id, body) =>
  call(`/admin/members/${encodeURIComponent(id)}/email/`, { method: 'POST', csrf, body });
export const resetMemberAuthenticator = (csrf, id, body) =>
  call(`/admin/members/${encodeURIComponent(id)}/reset/`, { method: 'POST', csrf, body });
export const exportMemberData = (csrf, id, body) =>
  call(`/admin/members/${encodeURIComponent(id)}/export/`, { method: 'POST', csrf, body });

// The Booking types and categories and the Roles (part 7.94). Every change
// answers the place as it is after the change (see setup.js). `list` is
// "booking-types" or "booking-categories"; a refusal carries its code.
export const readTypes = () => call('/admin/types/data/');
export const readRoles = () => call('/admin/roles/data/');
// body: {name, description} for a type, {name, icon} for a category; a change
// sends only what differs.
export const makeListItem = (csrf, list, body) => call(`/admin/types/${list}/make/`, { method: 'POST', csrf, body });
export const changeListItem = (csrf, list, id, body) =>
  call(`/admin/types/${list}/${encodeURIComponent(id)}/change/`, { method: 'POST', csrf, body });
// direction: "up" or "down", as the list is when the server gets it.
export const moveListItem = (csrf, list, id, direction) =>
  call(`/admin/types/${list}/${encodeURIComponent(id)}/move/`, { method: 'POST', csrf, body: { direction } });
export const removeListItem = (csrf, list, id) =>
  call(`/admin/types/${list}/${encodeURIComponent(id)}/remove/`, { method: 'POST', csrf });
// body: {name, categories: [ids], admin_access, standard} (see setup.js: roleBody).
export const makeRole = (csrf, body) => call('/admin/roles/make/', { method: 'POST', csrf, body });
export const changeRole = (csrf, id, body) =>
  call(`/admin/roles/${encodeURIComponent(id)}/change/`, { method: 'POST', csrf, body });
export const removeRole = (csrf, id) => call(`/admin/roles/${encodeURIComponent(id)}/remove/`, { method: 'POST', csrf });

// The Price books (part 7.97). Every change answers the place as it is after the
// change (see prices.js). body: {name, currency} for a book (a change sends only
// what differs); {entries: [{group, tiers_go_by, billing_basis, tiers: [{up_to,
// paid_per, amount}]}]} for the prices of a container in a booking category,
// saved and removed as a set. Amounts are in the book's smallest unit.
export const readPrices = () => call('/admin/prices/data/');
export const makePriceBook = (csrf, body) => call('/admin/prices/books/make/', { method: 'POST', csrf, body });
export const changePriceBook = (csrf, id, body) =>
  call(`/admin/prices/books/${encodeURIComponent(id)}/change/`, { method: 'POST', csrf, body });
export const removePriceBook = (csrf, id) => call(`/admin/prices/books/${encodeURIComponent(id)}/remove/`, { method: 'POST', csrf });
export const savePrices = (csrf, container, category, body) =>
  call(`/admin/prices/containers/${encodeURIComponent(container)}/${encodeURIComponent(category)}/save/`, { method: 'POST', csrf, body });
export const removePrices = (csrf, container, category, stamp) =>
  call(`/admin/prices/containers/${encodeURIComponent(container)}/${encodeURIComponent(category)}/remove/`, { method: 'POST', csrf, body: { stamp } });

// The Members place's Imports tab (part 7.93). query: page. The answer also
// lists the parsers an administrator can pick.
// The Payments place's Payouts tab (part 5.1). query: from, to, page (see payouts.js:
// payoutsQuery).
export const readPayouts = (query) => call('/admin/payments/payouts/list/', { query });
// One payout, by its id; query: payments_page, refunds_page. Reading it is looking
// it up: the server writes one "listed payouts" event.
export const readPayout = (id, query) => call(`/admin/payments/payouts/${encodeURIComponent(id)}/data/`, { query });
export const readImports = (query) => call('/admin/members/imports/list/', { query });
// One run, by its number. Reading it is looking it up: the server writes one
// "inspected" event.
export const readImport = (number) => call(`/admin/members/imports/${encodeURIComponent(number)}/data/`);
// Start a run: the parser's id and the file, sent as it is (never stored by
// the server). The answer is the run's page.
export const startImport = (csrf, parser, file) =>
  call('/admin/members/imports/start/', { method: 'POST', csrf, query: { parser, name: file.name }, raw: file });

// Add a member (part 8.2): body {full_name, email, phone, role, code} (see
// members.js: addBody). The answer is the new member's page.
export const addMember = (csrf, body) => call('/admin/members/add/', { method: 'POST', csrf, body });

// The Assets and Spaces (part 7.95). Every change answers {place, plan}: the
// place as it is after the change, and the floor plan it changed (or null).
// `body` is built by assets.js; a refusal carries its code and the model's words.
const ASSETS = '/admin/assets';
export const readAssets = () => call(`${ASSETS}/data/`);
export const readPlan = (id) => call(`${ASSETS}/plans/${encodeURIComponent(id)}/data/`);
const post = (csrf, path, body) => call(`${ASSETS}/${path}`, { method: 'POST', csrf, body });
export const makeSpace = (csrf, body) => post(csrf, 'spaces/make/', body);
export const changeSpace = (csrf, id, body) => post(csrf, `spaces/${encodeURIComponent(id)}/change/`, body);
export const removeSpace = (csrf, id) => post(csrf, `spaces/${encodeURIComponent(id)}/remove/`);
export const makeAsset = (csrf, body) => post(csrf, 'make/', body);
export const changeAsset = (csrf, id, body) => post(csrf, `${encodeURIComponent(id)}/change/`, body);
export const removeAsset = (csrf, id) => post(csrf, `${encodeURIComponent(id)}/remove/`);
export const makePlan = (csrf, asset, body) => post(csrf, `${encodeURIComponent(asset)}/plans/make/`, body);
export const changePlan = (csrf, plan, body) => post(csrf, `plans/${encodeURIComponent(plan)}/change/`, body);
export const removePlan = (csrf, plan) => post(csrf, `plans/${encodeURIComponent(plan)}/remove/`);
export const makeSpots = (csrf, plan, body) => post(csrf, `plans/${encodeURIComponent(plan)}/spots/make/`, body);
export const changeSpot = (csrf, plan, spot, body) =>
  post(csrf, `plans/${encodeURIComponent(plan)}/spots/${encodeURIComponent(spot)}/change/`, body);
export const removeSpot = (csrf, plan, spot) =>
  post(csrf, `plans/${encodeURIComponent(plan)}/spots/${encodeURIComponent(spot)}/remove/`);
export const makeGroup = (csrf, plan, body) => post(csrf, `plans/${encodeURIComponent(plan)}/groups/make/`, body);
export const changeGroup = (csrf, plan, group, body) =>
  post(csrf, `plans/${encodeURIComponent(plan)}/groups/${encodeURIComponent(group)}/change/`, body);
export const removeGroup = (csrf, plan, group) =>
  post(csrf, `plans/${encodeURIComponent(plan)}/groups/${encodeURIComponent(group)}/remove/`);

// The Containers (part 7.96). Every change answers the place as it is after the
// change (see containers.js); an add also answers `made`, the new container's id.
// A refusal of the validator is 409 "refused" with the model's `reasons`.
export const readContainers = () => call('/admin/containers/data/');
// body: {asset, booking_type, period_type, uses} (see containers.js: newBody).
export const makeContainer = (csrf, body) => call('/admin/containers/make/', { method: 'POST', csrf, body });
// body: what a sheet changed (see containers.js: detailsBody, timeBody) or {status}.
export const changeContainer = (csrf, id, body) =>
  call(`/admin/containers/${encodeURIComponent(id)}/change/`, { method: 'POST', csrf, body });
// body: {seen: [{category, hours}], categories: [{category, hours: [{weekday, start, end}]}]} (see containers.js: categoriesBody).
export const changeContainerCategories = (csrf, id, body) =>
  call(`/admin/containers/${encodeURIComponent(id)}/categories/`, { method: 'POST', csrf, body });
export const removeContainer = (csrf, id) =>
  call(`/admin/containers/${encodeURIComponent(id)}/remove/`, { method: 'POST', csrf });
// An asset's availability (part 7.98): weekly availabilities by start date,
// closures by date and the public holidays. A change is sent twice: with
// `preview: true` it answers {preview} (what it would cancel; nothing kept), then
// with `expect: preview.token` it is saved and the bookings it strands are
// cancelled and refunded: {saved, cancelled, unfinished, page}. A 409 "changed"
// carries the fresh `preview` (see availability.js).
const availability = (asset, path) => `${ASSETS}/${encodeURIComponent(asset)}/availability/${path}/`;
export const readAvailability = (asset) => call(availability(asset, 'data'));
// month: "2027-03"
export const readAvailabilityMonth = (asset, month) => call(availability(asset, 'month'), { query: { month } });
export const readHolidays = (asset, year) => call(availability(asset, 'holidays'), { query: { year } });
export const applyAvailability = (csrf, asset, body) => call(availability(asset, 'apply'), { method: 'POST', csrf, body });

// The Organization (part 7.99): its details with a version. A save sends
// {version, ...what differs}; a lowered retention is answered 409
// "confirm-erasure" with {erase: {members, bookings}}, which a second save sends
// back as "confirm". A refusal of a value is "invalid-<field>" with its reason;
// "stale" says someone saved since the sheet was opened.
export const readOrganization = () => call('/admin/organization/data/');
export const changeOrganization = (csrf, body) => call('/admin/organization/change/', { method: 'POST', csrf, body });

// Admin's events . query: when, from, to, category,
// asset, q, order, page (see events.js: eventsQuery). 25 events a page; the
// answer also holds the categories (key and the organization's word) and the
// assets a sheet chooses from.
export const readEvents = (query) => call('/admin/events/list/', { query });
// One event: its fields, contact, maker, a repeat's rule with its days, the days skipped.
export const readEvent = (id) => call(`/admin/events/${encodeURIComponent(id)}/data/`);
// The active members whose name holds `q`, at most twenty: who a contact can be.
export const readContacts = (query) => call('/admin/events/contacts/', { query });
// What an event would do, written nowhere. body: {asset, when, event, settle} (see
// events.js: previewBody).
export const previewEvent = (csrf, body) => call('/admin/events/preview/', { method: 'POST', csrf, body });
// body: the whole event (see events.js: eventBody). A make answers {made}; a
// change answers the event as readEvent does.
export const makeEvent = (csrf, body) => call('/admin/events/make/', { method: 'POST', csrf, body });
export const changeEvent = (csrf, id, body) =>
  call(`/admin/events/${encodeURIComponent(id)}/change/`, { method: 'POST', csrf, body });
export const removeEvent = (csrf, id) => call(`/admin/events/${encodeURIComponent(id)}/remove/`, { method: 'POST', csrf });
// One day of a repeat removed, or given other hours (body: {from, to}); both
// answer the event.
export const removeEventDay = (csrf, day) => call(`/admin/events/days/${encodeURIComponent(day)}/remove/`, { method: 'POST', csrf });
export const eventDayHours = (csrf, day, body) =>
  call(`/admin/events/days/${encodeURIComponent(day)}/hours/`, { method: 'POST', csrf, body });
// The event days on an asset a closure would be refused for (see events.js:
// inTheWayQuery, holidaysInTheWayQuery).
export const readEventDaysInTheWay = (query) => call('/admin/events/in-the-way/', { query });
