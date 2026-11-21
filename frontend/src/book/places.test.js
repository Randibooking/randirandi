// SPDX-License-Identifier: AGPL-3.0-only
import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  bookOpened, hrefOf, placeOf, staysInPage, titleOf,
} from './places.js';

const click = (extra = {}) => ({ defaultPrevented: false, button: 0, ...extra });

test('placeOf: the two addresses, nothing else', () => {
  assert.equal(placeOf('/book/'), 'book');
  assert.equal(placeOf('/me/'), 'me');
  assert.equal(placeOf('/admin/'), null);
  assert.equal(placeOf('/book'), null);
});

test('hrefOf and titleOf match the server pages', () => {
  assert.equal(hrefOf('book'), '/book/');
  assert.equal(hrefOf('me'), '/me/');
  assert.equal(titleOf('book'), 'Book · RandiRandi');
  assert.equal(titleOf('me'), 'My page · RandiRandi');
});

test('staysInPage: a plain left click only', () => {
  assert.equal(staysInPage(click(), { hasAttribute: () => false }), true);
  assert.equal(staysInPage(click({ metaKey: true })), false);
  assert.equal(staysInPage(click({ ctrlKey: true })), false);
  assert.equal(staysInPage(click({ shiftKey: true })), false);
  assert.equal(staysInPage(click({ altKey: true })), false);
  assert.equal(staysInPage(click({ button: 1 })), false);
  assert.equal(staysInPage(click({ defaultPrevented: true })), false);
  assert.equal(staysInPage(click(), { target: '_blank', hasAttribute: () => false }), false);
  assert.equal(staysInPage(click(), { hasAttribute: (name) => name === 'download' }), false);
});

test('bookOpened: not on a cold My page, kept once opened', () => {
  assert.equal(bookOpened(false, 'me'), false);
  assert.equal(bookOpened(false, 'book'), true);
  assert.equal(bookOpened(true, 'me'), true);
});

test('"Go to cart" is a link to the cart page the next part builds', async () => {
  const { CART_HREF } = await import('./places.js');
  assert.equal(CART_HREF, '/book/cart/');
});

import * as places from './places.js';
import {
  CART_HREF, PLACES, hrefFor, orderHref, placeOfView, placeWords, viewOf, viewTitle,
} from './places.js';

const ORDER = '5d7f0c3e-0000-4000-8000-000000000001';

test('viewOf: the two places, the cart and an order, nothing else', () => {
  assert.deepEqual(viewOf('/book/'), { view: 'book' });
  assert.deepEqual(viewOf('/me/'), { view: 'me' });
  assert.deepEqual(viewOf(CART_HREF), { view: 'cart' });
  assert.deepEqual(viewOf(`/book/order/${ORDER}/`), { view: 'order', order: ORDER });
  assert.equal(viewOf('/book/order/x/'), null);
  assert.equal(viewOf(`/book/order/${ORDER}`), null);
  assert.equal(viewOf('/book/cart'), null);
});

test('the cart and the outcome belong to Book', () => {
  assert.equal(placeOfView('cart'), 'book');
  assert.equal(placeOfView('order'), 'book');
  assert.equal(placeOfView('me'), 'me');
  assert.equal(viewTitle('cart'), 'Cart · RandiRandi');
  assert.equal(viewTitle('book'), 'Book · RandiRandi');
});

test('hrefFor is the inverse of viewOf', () => {
  for (const view of [{ view: 'book' }, { view: 'me' }, { view: 'cart' }, { view: 'order', order: ORDER }]) {
    assert.deepEqual(viewOf(hrefFor(view)), view);
  }
  assert.equal(orderHref(ORDER), `/book/order/${ORDER}/`);
});

test('the navigation holds Book, Events and My page, the same for a member and an administrator', () => {
  assert.deepEqual(PLACES, [['book', '/book/'], ['events', '/events/'], ['me', '/me/']]);
  assert.equal(PLACES.length, 3);
  assert.equal(places.placesFor, undefined);
  assert.equal(places.ADMIN_PLACE, undefined);
  assert.deepEqual(PLACES.map(([key]) => placeWords(key)), ['Book', 'Events', 'My page']);
});

test('Admin is never a view of the member shell', () => {
  assert.equal(placeOf('/admin/'), null);
  assert.equal(viewOf('/admin/'), null);
});

test('the prices page is a view of Book at /book/prices/', () => {
  assert.deepEqual(viewOf('/book/prices/'), { view: 'prices' });
  assert.equal(hrefFor({ view: 'prices' }), '/book/prices/');
  assert.equal(viewTitle('prices'), 'Prices · RandiRandi');
  assert.equal(placeOfView('prices'), 'book');
});

test('Events is a place between Book and My page; an event is a view of it', () => {
  assert.deepEqual(viewOf('/events/'), { view: 'events' });
  assert.deepEqual(viewOf('/events/12/'), { view: 'event', event: 12 });
  assert.equal(viewOf('/events/0/'), null);
  assert.equal(viewOf('/events/12'), null);
  assert.equal(viewOf('/events/x/'), null);
  assert.equal(hrefFor({ view: 'event', event: 12 }), '/events/12/');
  for (const view of [{ view: 'events' }, { view: 'event', event: 3 }]) assert.deepEqual(viewOf(hrefFor(view)), view);
  assert.equal(placeOfView('events'), 'events');
  assert.equal(placeOfView('event'), 'events');
  assert.equal(viewTitle('event'), 'Events · RandiRandi');
  assert.equal(titleOf('events'), 'Events · RandiRandi');
});

// Admin and Sign out are on My page only : no other member
// page links either. The shell holds the sign-out form itself, which My page's row submits.
test('no member page but My page links Admin or Sign out', async () => {
  const { readdirSync, readFileSync } = await import('node:fs');
  const here = new URL('./', import.meta.url);
  const leaks = readdirSync(here)
    .filter((name) => /\.jsx?$/.test(name) && !name.endsWith('.test.js') && !['MyPage.jsx', 'mine.js'].includes(name))
    .filter((name) => {
      const code = readFileSync(new URL(name, here), 'utf8').replace(/\/\/[^\n]*/g, '');
      const shell = name === 'MemberShell.jsx';
      // (An event's own edit link in the admin is not the navigation.) The shell's own form is the one place outside My page that names the logout address.
      return /['"`]\/admin\/['"`]|place\.admin|ADMIN_PLACE/.test(code) || /me\.sign-out/.test(code)
        || (!shell && /\/logout\//.test(code));
    });
  assert.deepEqual(leaks, []);
});
