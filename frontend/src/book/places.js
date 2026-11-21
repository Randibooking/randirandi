// SPDX-License-Identifier: AGPL-3.0-only
// The member shell's places, switched inside the page (part 3.56); Events between Book and My page ().
// The hrefs stay real, so a new tab and a reload still work.
import { label } from './labels.js';

// Each place: its key and its address.
export const PLACES = [['book', '/book/'], ['events', '/events/'], ['me', '/me/']];

// The place's name in the page's words: `place.book`, `place.events`, `place.me`.
export const placeWords = (place) => label(`place.${place}`);

// The place an address names, or null for any other path.
export function placeOf(pathname) {
  const found = PLACES.find(([, href]) => href === pathname);
  return found ? found[0] : null;
}

export const hrefOf = (place) => PLACES.find(([key]) => key === place)[1];

const titled = (page) => label('title.page', { page, product: label('shell.product') });

export const titleOf = (place) => titled(placeWords(place));

// A plain left click keeps the page; anything the browser has its own way
// with (new tab, new window, download, another target) goes on as a link.
export function staysInPage(event, link) {
  if (event.defaultPrevented || event.button !== 0) return false;
  if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return false;
  if (link?.target && link.target !== '_self') return false;
  return !link?.hasAttribute?.('download');
}

// Book is built on the first visit to it and kept after: a cold My page asks
// the server nothing for Book.
export const bookOpened = (opened, place) => opened || place === 'book';

// "Go to cart" on the Book page, and the pages after it (part 3.7). Book stays
// the chosen place up to paying; the cart and an order's outcome are views of it.
export const CART_HREF = '/book/cart/';

// The prices (), a view of Book like the cart.
export const PRICES_HREF = '/book/prices/';

// An event's page in Events ().
const EVENT_PATH = /^\/events\/([1-9][0-9]{0,8})\/$/;
export const eventHref = (event) => `/events/${event}/`;

// A view opened from a part of the page the shell's links do not reach (an
// event on Book): the address is pushed and the shell follows it as it
// follows Back.
export function goTo(href) {
  const history = window.history;
  const current = viewOf(window.location.pathname);
  const next = viewOf(href.split('?')[0]);
  const here = window.location.pathname + window.location.search + window.location.hash;
  rememberScroll();
  // Keep the entry's own position, including the calendar's query. Its React
  // state stays mounted; returning through history restores this entry.
  history.replaceState({ ...history.state, rrScroll: [window.scrollX, window.scrollY] }, '', here);
  let origin = null;
  if (next?.view === 'event') {
    if (current?.view === 'book' || current?.view === 'events') origin = { href: here, steps: 1 };
    else if (current?.view === 'event' && eventOrigin()) origin = { ...eventOrigin(), steps: eventOrigin().steps + 1 };
  }
  history.pushState(origin ? { rrEventOrigin: origin } : null, '', href);
  window.scrollTo(0, 0);
  window.dispatchEvent(new PopStateEvent('popstate'));
}

// Only our two entry pages can be return targets. A direct event link has no
// origin and falls back to Events, without guessing from a referrer.
export function eventOrigin() {
  const origin = window.history.state?.rrEventOrigin;
  if (!origin || !Number.isSafeInteger(origin.steps) || origin.steps < 1
    || !/^\/(book|events)\/(?:[?#].*)?$/.test(origin.href)) return null;
  return origin;
}

export function eventBack() {
  const origin = eventOrigin();
  const href = origin?.href || '/events/';
  return {
    href,
    calendar: href.startsWith('/book/'),
    onClick: (event) => {
      if (!staysInPage(event, event.currentTarget)) return;
      event.preventDefault();
      if (origin) window.history.go(-origin.steps);
      else goTo(href);
    },
  };
}

// Keep live positions outside history to avoid repeated replaceState calls
// during a drag/scroll. pagehide flushes the active entry for a reload.
const scrollPositions = new Map();
let restoring = false;
function scrollEntry() {
  if (!window.history.state?.rrEntry) window.history.replaceState({ ...window.history.state, rrEntry: crypto.randomUUID() }, '');
  return window.history.state.rrEntry;
}
function rememberScroll() {
  if (restoring || !window.history.state?.rrEntry) return;
  const entry = window.history.state.rrEntry;
  const position = [window.scrollX, window.scrollY];
  scrollPositions.set(entry, position);
  // Browsers can snapshot history before pagehide finishes. Session storage
  // carries the latest position through reload; blocked storage falls back to
  // the live map and the history entry saved by goTo/pagehide.
  try { window.sessionStorage?.setItem(`rr-scroll:${entry}`, JSON.stringify(position)); } catch { /* Storage unavailable. */ }
}
export function trackScroll() {
  scrollEntry();
  const flush = () => {
    if (restoring) return;
    rememberScroll();
    window.history.replaceState({ ...window.history.state, rrScroll: [window.scrollX, window.scrollY] }, '');
  };
  window.addEventListener('scroll', rememberScroll, { passive: true });
  window.addEventListener('pagehide', flush);
  return () => {
    window.removeEventListener('scroll', rememberScroll);
    window.removeEventListener('pagehide', flush);
  };
}
export const stopScrollRestore = () => { restoring = false; };
export function restoreScroll() {
  const entry = scrollEntry();
  let kept = null;
  try { kept = JSON.parse(window.sessionStorage?.getItem(`rr-scroll:${entry}`) || 'null'); } catch { /* Storage unavailable. */ }
  const position = scrollPositions.get(entry) || kept || window.history.state?.rrScroll;
  restoring = true;
  if (Array.isArray(position) && position.length === 2 && position.every(Number.isFinite)) {
    window.scrollTo(...position);
    const done = Math.abs(window.scrollX - position[0]) < 1 && Math.abs(window.scrollY - position[1]) < 1;
    if (done) restoring = false;
    return done;
  }
  restoring = false;
  return true;
}

// Each calendar history entry owns its view, dates and filter, even when
// another visit changes the mounted Book or the event page is reloaded.
export const bookState = () => window.history.state?.rrBook || null;
export function keepBookState(state) {
  if (window.location.pathname !== '/book/') return;
  window.history.replaceState({ ...window.history.state, rrBook: state }, '');
}

export const inPage = (href) => ({
  href,
  onClick: (event) => {
    if (!staysInPage(event, event.currentTarget)) return;
    event.preventDefault();
    goTo(href);
  },
});

// Book on a day asked from elsewhere (an event's "See it in Book"): kept
// until Book takes it, on its first visit or at once when it is there.
let asked = null;
export function showDay(date) {
  asked = date;
  goTo('/book/');
  window.dispatchEvent(new CustomEvent('rr-show-day'));
}
export const takeDay = () => {
  const day = asked;
  asked = null;
  return day;
};

const ORDER_PATH = /^\/book\/order\/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\/$/i;

export const orderHref = (order) => `/book/order/${order}/`;

// The view an address names: {view: 'book' | 'me' | 'cart' | 'order', order?},
// or null for any other path.
export function viewOf(pathname) {
  const place = placeOf(pathname);
  if (place) return { view: place };
  if (pathname === CART_HREF) return { view: 'cart' };
  if (pathname === PRICES_HREF) return { view: 'prices' };
  const event = EVENT_PATH.exec(pathname);
  if (event) return { view: 'event', event: Number(event[1]) };
  const found = ORDER_PATH.exec(pathname);
  return found ? { view: 'order', order: found[1].toLowerCase() } : null;
}

// The place whose button is chosen: the cart and the outcome belong to Book.
export const placeOfView = (view) => {
  if (view === 'me') return 'me';
  return view === 'events' || view === 'event' ? 'events' : 'book';
};

export const viewTitle = (view) => {
  if (view === 'cart') return titled(label('title.cart'));
  if (view === 'prices') return titled(label('title.prices'));
  if (view === 'order') return titled(label('title.order'));
  if (view === 'conditions') return titled(label('shell.conditions'));
  if (view === 'event') return titled(label('title.events'));
  return titleOf(view);
};

// The address of a view: {view, order?}.
export function hrefFor({ view, order, event }) {
  if (view === 'event') return eventHref(event);
  if (view === 'cart') return CART_HREF;
  if (view === 'prices') return PRICES_HREF;
  if (view === 'order') return orderHref(order);
  return hrefOf(view);
}
