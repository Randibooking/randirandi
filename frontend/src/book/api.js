// SPDX-License-Identifier: AGPL-3.0-only
// The browser's calls to the member booking server (randirandi/core/booking_views.py).
// Every function answers one of:
//   {ok: true, data}                      data is null for a 204
//   {kind: 'signed-out'}                  a redirect on to the login page
//   {kind: 'forbidden', status: 403}      a stale token or a signed-in non-member
//   {kind: 'refused', status, code, text, body}   the server's own fields
//   {kind: 'offline'}                     the request never got an answer
// A change sends `body` as JSON, `form` as a form, or `raw` (a File) as it is.
// A change without the page's CSRF token is a TypeError, never sent.
// A refusal's `text` is the label of its code when the code is a label key (the
// organization's words, else the default), otherwise the server's own text.
import { serverWords } from './labels.js';

export async function call(path, { query, method = 'GET', csrf, body, form, raw } = {}) {
  const params = new URLSearchParams();
  for (const [name, value] of Object.entries(query || {})) {
    if (value === undefined || value === null) continue;
    params.set(name, Array.isArray(value) ? value.join(',') : String(value));
  }
  const search = params.toString();
  const headers = { Accept: 'application/json' };
  const init = { method, headers, credentials: 'same-origin' };
  if (method !== 'GET') {
    if (!csrf) throw new TypeError('A change needs the page\'s CSRF token.');
    headers['X-CSRFToken'] = csrf;
    if (raw !== undefined) {
      // A file, sent as it is: the browser reads it from disk, nothing is built in memory.
      headers['Content-Type'] = 'application/octet-stream';
      init.body = raw;
    } else if (form) {
      headers['Content-Type'] = 'application/x-www-form-urlencoded';
      init.body = new URLSearchParams(form).toString();
    } else {
      headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(body ?? {});
    }
  }
  let response;
  try {
    response = await fetch(search ? `${path}?${search}` : path, init);
  } catch {
    return { kind: 'offline' };
  }
  if (response.redirected && new URL(response.url, 'http://x').pathname === '/login/') {
    return { kind: 'signed-out' };
  }
  if (response.status === 403) return { kind: 'forbidden', status: 403 };
  let data = null;
  if (response.status !== 204) {
    try {
      data = await response.json();
    } catch {
      data = undefined;
    }
  }
  if (response.ok && data !== undefined) return { ok: true, data };
  const body_ = data && typeof data === 'object' ? data : {};
  return {
    kind: 'refused',
    status: response.status,
    code: body_.error ?? body_.reason ?? null,
    text: serverWords(body_.error ?? body_.reason, body_.text ?? null, body_),
    body: body_,
  };
}

export const readChoices = () => call('/booking/choices/');

export const saveCategoryChoice = (csrf, category) =>
  call('/booking/choices/category/', { method: 'POST', csrf, form: { category } });

// query: start, days, and asset or space; maybe type, category, quantity,
// group, length, editing; `prices: 1` is the standard role's answer (the price list)
export const readCalendar = (query) => call('/booking/calendar/', { query });

// query: container, category, start, end; maybe quantity, spots (array),
// group, editing
// A time that cannot be booked is a 200 with available: false.
export async function readPrice(query) {
  const result = await call('/booking/price/', { query });
  if (result.ok && result.data.available === false) {
    const { reason, text } = result.data;
    return {
      kind: 'refused', status: 200, code: reason ?? null, text: text ?? null, body: result.data,
    };
  }
  return result;
}

export const readCart = () => call('/booking/cart/');

// The organization's events (randirandi/core/event_member_views.py): query
// when, category (array), asset, or event for one.
export const readEvents = (query) => call('/events/data/', { query });

// booking: container, category, quantity, start, end, total; maybe spots, group
export const addToCart = (csrf, booking) =>
  call('/booking/cart/add/', { method: 'POST', csrf, body: booking });

export const readMyBookings = () => call('/booking/bookings/');

export const removeFromCart = (csrf, id) =>
  call(`/booking/cart/${encodeURIComponent(id)}/remove/`, { method: 'POST', csrf });

// Make one order from the cart at the total the page shows, accepting the
// booking conditions the cart showed : the server is the
// gate and refuses an order without it.
export const orderCart = (csrf, total, acceptedVersion) =>
  call('/booking/cart/order/', { method: 'POST', csrf, body: { total, accepted_version: acceptedVersion } });

// The public price list : the standard role's answers, no member in them.
export const readPublicChoices = () => call('/prices/choices/');
export const readPublicCalendar = (query) => call('/prices/calendar/', { query });

// Start (or start again) the payment of a held order.
export const startPayment = (csrf, order) =>
  call(`/payments/start/${encodeURIComponent(order)}/`, { method: 'POST', csrf });

// Let go of a held order: the answer is the cart, its times back in it.
export const cancelOrder = (csrf, order) =>
  call(`/booking/order/${encodeURIComponent(order)}/cancel/`, { method: 'POST', csrf });

export const readOrder = (order) => call(`/booking/order/${encodeURIComponent(order)}/`);
