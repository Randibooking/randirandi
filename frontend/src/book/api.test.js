// SPDX-License-Identifier: AGPL-3.0-only
import { afterEach, test } from 'node:test';
import assert from 'node:assert/strict';
import {
  addToCart, call, readCalendar, readCart, readChoices, readMyBookings, readPrice,
  saveCategoryChoice,
} from './api.js';
import { setLabels } from './labels.js';

const real = globalThis.fetch;
afterEach(() => { globalThis.fetch = real; setLabels({}); });

function answer(status, body, extra = {}) {
  const calls = [];
  globalThis.fetch = async (url, init) => {
    calls.push({ url, init });
    return {
      status, ok: status >= 200 && status < 300, redirected: false,
      json: async () => { if (body === undefined) throw new Error('no body'); return body; },
      ...extra,
    };
  };
  return calls;
}

test('readChoices: GET, no body, no token', async () => {
  const calls = answer(200, { categories: [] });
  assert.deepEqual(await readChoices(), { ok: true, data: { categories: [] } });
  assert.equal(calls[0].url, '/booking/choices/');
  assert.equal(calls[0].init.method, 'GET');
  assert.equal(calls[0].init.credentials, 'same-origin');
  assert.equal(calls[0].init.headers.Accept, 'application/json');
  assert.equal(calls[0].init.headers['X-CSRFToken'], undefined);
  assert.equal(calls[0].init.body, undefined);
});

test('saveCategoryChoice: form POST with token, 204 is ok with null data', async () => {
  const calls = answer(204);
  assert.deepEqual(await saveCategoryChoice('tok', 7), { ok: true, data: null });
  assert.equal(calls[0].url, '/booking/choices/category/');
  assert.equal(calls[0].init.method, 'POST');
  assert.equal(calls[0].init.headers['X-CSRFToken'], 'tok');
  assert.equal(calls[0].init.headers['Content-Type'], 'application/x-www-form-urlencoded');
  assert.equal(calls[0].init.body, 'category=7');
});

test('saveCategoryChoice: 404 not-offered is refused', async () => {
  answer(404, { error: 'not-offered' });
  assert.deepEqual(await saveCategoryChoice('tok', 7), {
    kind: 'refused', status: 404, code: 'not-offered', text: 'not offered', body: { error: 'not-offered' },
  });
});

test('readCalendar: query built, empty values left out, encoded', async () => {
  const calls = answer(200, { containers: [] });
  await readCalendar({ asset: 3, start: '2026-10-19', days: 7, type: undefined, category: null, editing: 'ab' });
  assert.equal(calls[0].url, '/booking/calendar/?asset=3&start=2026-10-19&days=7&editing=ab');
  assert.equal(calls[0].init.method, 'GET');
});

test('readCalendar: 422 carries the server code', async () => {
  answer(422, { error: 'fewer-days' });
  const few = await readCalendar({ start: '2026-10-19', days: 99 });
  assert.equal(few.kind, 'refused');
  assert.equal(few.status, 422);
  assert.equal(few.code, 'fewer-days');
});

test('readPrice: spots joined by comma, moments URL-encoded', async () => {
  const calls = answer(200, { available: true });
  await readPrice({
    container: 1, category: 2, quantity: 1, spots: [4, 5],
    start: '2026-10-19T10:00:00+02:00', end: '2026-10-19T11:00:00+02:00',
  });
  const url = new URL(calls[0].url, 'http://x');
  assert.equal(url.pathname, '/booking/price/');
  assert.equal(url.searchParams.get('spots'), '4,5');
  assert.equal(url.searchParams.get('start'), '2026-10-19T10:00:00+02:00');
  assert.match(calls[0].url, /start=2026-10-19T10%3A00%3A00%2B02%3A00/);
});

test('readCart: GET /booking/cart/', async () => {
  const calls = answer(200, { count: 0 });
  assert.deepEqual(await readCart(), { ok: true, data: { count: 0 } });
  assert.equal(calls[0].url, '/booking/cart/');
});

test('addToCart: JSON POST with token', async () => {
  const calls = answer(200, { count: 1 });
  const booking = { container: 1, category: 2, quantity: 1, start: 'a', end: 'b', total: 0 };
  assert.deepEqual(await addToCart('tok', booking), { ok: true, data: { count: 1 } });
  assert.equal(calls[0].url, '/booking/cart/add/');
  assert.equal(calls[0].init.method, 'POST');
  assert.equal(calls[0].init.headers['X-CSRFToken'], 'tok');
  assert.equal(calls[0].init.headers['Content-Type'], 'application/json');
  assert.deepEqual(JSON.parse(calls[0].init.body), booking);
});

test('addToCart: 409 keeps reason and total; its text is the reason\'s label', async () => {
  const body = { reason: 'price-changed', text: 'The price changed.', total: 900 };
  answer(409, body);
  assert.deepEqual(await addToCart('tok', {}), {
    kind: 'refused', status: 409, code: 'price-changed', text: 'the price has changed', body,
  });
});

test('a refused order for the day\'s limit shows the server\'s counted text, in the organization\'s language', async () => {
  setLabels({});
  const text = 'Du kan gjøre 5 bookinger per dag. Du har 2 igjen i dag.';
  answer(409, { reason: 'day-limit', text, left: 2, limit: 5 });
  assert.equal((await addToCart('tok', {})).text, text);
});

test('a refusal says the organization\'s words for its reason, and the server\'s text for any other code', async () => {
  setLabels({ taken: 'already gone' });
  answer(409, { reason: 'taken', text: 'taken' });
  assert.equal((await addToCart('tok', {})).text, 'already gone');
  answer(422, { error: 'fewer-days', text: 'Ask for fewer days.' });
  assert.equal((await readCalendar({ start: '2026-10-19', days: 99 })).text, 'Ask for fewer days.');
  answer(400, { error: 'bad-request' });
  assert.equal((await addToCart('tok', {})).text, null);
});

test('addToCart: 400 bad-request', async () => {
  answer(400, { error: 'bad-request' });
  const result = await addToCart('tok', {});
  assert.equal(result.code, 'bad-request');
  assert.equal(result.text, null);
});

test('readMyBookings: GET /booking/bookings/', async () => {
  const calls = answer(200, { coming: [], earlier: [] });
  assert.equal((await readMyBookings()).ok, true);
  assert.equal(calls[0].url, '/booking/bookings/');
});

test('403 with an HTML page (stale token) is forbidden', async () => {
  answer(403, undefined);
  assert.deepEqual(await addToCart('tok', {}), { kind: 'forbidden', status: 403 });
});

test('403 with an empty body (not a member) is forbidden', async () => {
  answer(403, undefined, { json: async () => { throw new SyntaxError('empty'); } });
  assert.deepEqual(await readCart(), { kind: 'forbidden', status: 403 });
});

test('signed out: followed redirect to the login page', async () => {
  answer(200, undefined, { redirected: true, url: 'https://x.test/login/?next=/booking/cart/' });
  assert.deepEqual(await readChoices(), { kind: 'signed-out' });
});

test('a redirect elsewhere is not signed-out', async () => {
  answer(200, undefined, { redirected: true, url: 'https://x.test/elsewhere/' });
  assert.equal((await readChoices()).kind, 'refused');
});

test('readPrice: available false is refused with reason and text', async () => {
  const body = { available: false, reason: 'taken', text: 'Taken.', places_left: 0 };
  answer(200, body);
  assert.deepEqual(await readPrice({ container: 1 }), {
    kind: 'refused', status: 200, code: 'taken', text: 'Taken.', body,
  });
});

test('readPrice: an available price is ok', async () => {
  answer(200, { available: true, total: 100 });
  assert.deepEqual(await readPrice({ container: 1 }), { ok: true, data: { available: true, total: 100 } });
});

test('network failure is offline', async () => {
  globalThis.fetch = async () => { throw new TypeError('Failed to fetch'); };
  assert.deepEqual(await readCart(), { kind: 'offline' });
  assert.deepEqual(await saveCategoryChoice('tok', 1), { kind: 'offline' });
});

test('a 500 with no JSON is refused with null code and text', async () => {
  answer(500, undefined);
  assert.deepEqual(await readCart(), { kind: 'refused', status: 500, code: null, text: null, body: {} });
});

test('a 200 that is not JSON is refused, not ok', async () => {
  answer(200, undefined);
  assert.equal((await readCart()).kind, 'refused');
});

test('a change without a CSRF token is refused before it is sent', async () => {
  const calls = answer(204);
  await assert.rejects(saveCategoryChoice(undefined, 7), TypeError);
  await assert.rejects(addToCart('', { container: 1 }), TypeError);
  assert.equal(calls.length, 0);
});

import { orderCart, readOrder, removeFromCart, startPayment } from './api.js';

test('removeFromCart, orderCart, startPayment: POST with the token; readOrder: GET', async () => {
  let calls = answer(200, { count: 0 });
  await removeFromCart('tok', 'a1');
  assert.equal(calls[0].url, '/booking/cart/a1/remove/');
  assert.equal(calls[0].init.method, 'POST');
  assert.equal(calls[0].init.headers['X-CSRFToken'], 'tok');
  calls = answer(200, { order: 'o', confirmed: true });
  await orderCart('tok', 250, 'abc123');
  assert.equal(calls[0].url, '/booking/cart/order/');
  assert.deepEqual(JSON.parse(calls[0].init.body), { total: 250, accepted_version: 'abc123' });
  calls = answer(200, { redirect: 'https://vipps.test/x' });
  await startPayment('tok', 'o');
  assert.equal(calls[0].url, '/payments/start/o/');
  assert.equal(calls[0].init.method, 'POST');
  calls = answer(200, { id: 'o' });
  await readOrder('o');
  assert.equal(calls[0].url, '/booking/order/o/');
  assert.equal(calls[0].init.method, 'GET');
});

test('a refused order keeps the order id in the body, and a missing token is never sent', async () => {
  answer(409, { order: 'o', reason: 'payment-unavailable' });
  const result = await orderCart('tok', 5);
  assert.equal(result.kind, 'refused');
  assert.equal(result.code, 'payment-unavailable');
  assert.equal(result.body.order, 'o');
  await assert.rejects(() => orderCart('', 5), TypeError);
});

test('a change with raw sends the file as it is, with the token and the query', async () => {
  const calls = answer(200, { ok: 1 });
  const file = { name: 'members.xlsx', size: 3 };
  assert.deepEqual(await call('/admin/members/imports/start/', {
    method: 'POST', csrf: 'tok', query: { parser: 'p', name: 'members.xlsx' }, raw: file,
  }), { ok: true, data: { ok: 1 } });
  assert.equal(calls[0].url, '/admin/members/imports/start/?parser=p&name=members.xlsx');
  assert.equal(calls[0].init.body, file);
  assert.equal(calls[0].init.headers['Content-Type'], 'application/octet-stream');
  assert.equal(calls[0].init.headers['X-CSRFToken'], 'tok');
});

test('the public price list reads the public answers, with no token and no member', async () => {
  const { readPublicCalendar, readPublicChoices } = await import('./api.js');
  let calls = answer(200, { assets: [] });
  await readPublicChoices();
  assert.equal(calls[0].url, '/prices/choices/');
  assert.equal(calls[0].init.method, 'GET');
  calls = answer(200, { containers: [] });
  await readPublicCalendar({ asset: 1, start: '2026-11-02', days: 14, prices: 1 });
  assert.match(calls[0].url, /^\/prices\/calendar\/\?/);
  assert.equal(calls[0].init.method, 'GET');
});
