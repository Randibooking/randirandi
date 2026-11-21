// SPDX-License-Identifier: AGPL-3.0-only
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { bookState, eventBack, eventOrigin, goTo, keepBookState, restoreScroll, trackScroll } from './places.js';

// Exercise the real navigation functions against a browser-shaped history,
// including actual popstate dispatch and preserving the original entry.
function browser(t, href) {
  const old = { window: globalThis.window, PopStateEvent: globalThis.PopStateEvent };
  const events = new EventTarget();
  const entries = [{ href, state: null }];
  let index = 0;
  const win = {
    location: new URL(href, 'https://example.test'),
    scrollX: 0, scrollY: 370,
    scrollTo(x, y) { this.scrollX = x; this.scrollY = y; },
    dispatchEvent: (event) => events.dispatchEvent(event),
    addEventListener: (...args) => events.addEventListener(...args),
    removeEventListener: (...args) => events.removeEventListener(...args),
    history: {
      get state() { return entries[index].state; },
      replaceState(state, _, address = win.location.href) { entries[index] = { state, href: address }; win.location = new URL(address, win.location); },
      pushState(state, _, address) {
        entries.splice(index + 1, Infinity, { state, href: address });
        index += 1; win.location = new URL(address, win.location);
      },
      go(step) {
        index += step; win.location = new URL(entries[index].href, win.location);
        events.dispatchEvent(new Event('popstate'));
      },
    },
  };
  globalThis.window = win;
  globalThis.PopStateEvent = Event;
  events.addEventListener('popstate', restoreScroll);
  const untrack = trackScroll();
  t.after(untrack);
  t.after(() => { globalThis.window = old.window; globalThis.PopStateEvent = old.PopStateEvent; });
  return win;
}
const click = (extra = {}) => ({ button: 0, preventDefault() {}, ...extra });

test('calendar event returns to its original query and scroll, through popstate', (t) => {
  const win = browser(t, '/book/?category=2&asset=3');
  goTo('/events/12/');
  assert.equal(win.scrollY, 0);
  assert.equal(eventBack().calendar, true);
  assert.equal(eventBack().href, '/book/?category=2&asset=3');
  eventBack().onClick(click());
  assert.equal(win.location.pathname + win.location.search, '/book/?category=2&asset=3');
  assert.equal(win.scrollY, 370);
});

test('filtered event list returns past a chain of event selections', (t) => {
  const win = browser(t, '/events/?when=past&where=2');
  goTo('/events/12/');
  goTo('/events/13/');
  assert.equal(eventOrigin().steps, 2);
  assert.equal(eventBack().calendar, false);
  eventBack().onClick(click());
  assert.equal(win.location.pathname + win.location.search, '/events/?when=past&where=2');
  assert.equal(win.scrollY, 370);
});

test('a second calendar visit cannot overwrite the event origin calendar', (t) => {
  const win = browser(t, '/book/?category=2');
  const first = { calendar: 'month', chosen: '2026-11-03', anchors: { month: '2026-11' }, pick: { category: 2 } };
  keepBookState(first);
  goTo('/events/12/');
  keepBookState({ calendar: 'week' }); // hidden Book does not write to an event
  assert.equal(bookState(), null);
  goTo('/book/');
  keepBookState({ calendar: 'week', chosen: '2026-12-01', anchors: { week: '2026-12-01' }, pick: { category: 1 } });
  win.history.go(-1);
  eventBack().onClick(click());
  assert.deepEqual(bookState(), first);
  assert.equal(win.location.search, '?category=2');
});

test('direct event links fall back to Events and modified clicks retain browser behavior', (t) => {
  const win = browser(t, '/events/12/');
  assert.equal(eventOrigin(), null);
  const back = eventBack();
  assert.equal(back.href, '/events/');
  back.onClick(click({ metaKey: true, preventDefault() { assert.fail('modified click intercepted'); } }));
  assert.equal(win.location.pathname, '/events/12/');
  back.onClick(click());
  assert.equal(win.location.pathname, '/events/');
});

test('an untrusted origin cannot turn the return link into an external navigation', (t) => {
  const win = browser(t, '/events/12/');
  win.history.replaceState({ rrEventOrigin: { href: 'https://elsewhere.test/', steps: 1 } }, '', '/events/12/');
  assert.equal(eventOrigin(), null);
  assert.equal(eventBack().href, '/events/');
});

test('native history returns use the latest scroll and pagehide keeps it for reload', (t) => {
  const win = browser(t, '/book/');
  goTo('/events/12/');
  eventBack().onClick(click());
  win.scrollY = 500;
  win.dispatchEvent(new Event('scroll'));
  win.history.go(1);
  win.history.go(-1);
  assert.equal(win.scrollY, 500);
  win.scrollY = 620;
  win.dispatchEvent(new Event('pagehide'));
  assert.deepEqual(win.history.state.rrScroll, [0, 620]);
});

test('reload uses the stored position and unavailable storage falls back to history', (t) => {
  const win = browser(t, '/book/');
  win.history.replaceState({ rrEntry: 'reload-entry', rrScroll: [0, 10] }, '');
  win.sessionStorage = { getItem: () => '[0,75]' };
  assert.equal(restoreScroll(), true);
  assert.equal(win.scrollY, 75);
  win.sessionStorage.getItem = () => { throw new Error('storage blocked'); };
  assert.equal(restoreScroll(), true);
  assert.equal(win.scrollY, 10);
});
