// SPDX-License-Identifier: AGPL-3.0-only
// Answered month calendars kept in memory (part 3.56), so a switch to a period
// already seen shows at once. Pure: no DOM, no fetch.
// A kept answer belongs to its full question: every field of the query, the
// first day and the number of days. Nothing else finds it.

export const KEPT = 12;
export const MAX_AGE = 5 * 60 * 1000;
export const FIRST_DELAY = 300;

export function keyOf(query, start, days) {
  const fields = Object.entries(query || {})
    .filter(([, value]) => value !== undefined && value !== null)
    .sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0));
  return JSON.stringify([fields, start, days]);
}

// Least recently used goes first when more than `limit` are kept. An answer
// older than `maxAge` ms is not kept: it reads as absent. `now` is the clock.
export function createStore(limit = KEPT, now = Date.now, maxAge = MAX_AGE) {
  const kept = new Map();
  // The asks on their way, by question (part 3.997): a second ask for a question
  // already on its way takes that answer, so one switch never sends it twice.
  const flying = new Map();
  let generation = 0;
  const fresh = (key) => {
    const row = kept.get(key);
    if (!row) return false;
    if (now() - row.at <= maxAge) return true;
    kept.delete(key);
    return false;
  };
  return {
    get(key) {
      if (!fresh(key)) return undefined;
      const row = kept.get(key);
      kept.delete(key);
      kept.set(key, row);
      return row.value;
    },
    has: (key) => fresh(key),
    put(key, value) {
      kept.delete(key);
      kept.set(key, { value, at: now() });
      while (kept.size > limit) kept.delete(kept.keys().next().value);
    },
    // `start()` asks the server; its promise is shared by every ask for `key`
    // until it settles. A cart or booking change drops the asks on their way
    // too, and an answer asked before the change is never handed on: the ask is
    // sent again (twice at most), then answers `{kind: 'stale'}`. A `start` that
    // throws or rejects answers `{kind: 'offline'}`, so no caller sees a rejection.
    async ask(key, start) {
      for (let tries = 0; ; tries += 1) {
        const born = generation;
        if (!flying.has(key)) {
          const run = Promise.resolve().then(start).catch(() => ({ kind: 'offline' })).finally(() => {
            if (flying.get(key) === run) flying.delete(key);
          });
          flying.set(key, run);
        }
        const result = await flying.get(key);
        if (born === generation) return result;
        if (tries >= 2) return { kind: 'stale' };
      }
    },
    // After a cart or booking change: every kept answer is old.
    clear: () => { kept.clear(); flying.clear(); generation += 1; },
    get size() { return kept.size; },
  };
}

// The months to fetch quietly, the one in the direction of travel first.
export function neighbours(month, direction, shift, back) {
  const ahead = shift(month, 1);
  const behind = back ? shift(month, -1) : null;
  const order = direction < 0 ? [behind, ahead] : [ahead, behind];
  return order.filter((name) => name !== null);
}

// One at a time, after `delay` ms; each only while `still()` says the member
// is on the period that asked, and only if it is not already kept.
export async function prefetch({ names, has, fetch, still, wait, delay = FIRST_DELAY }) {
  await wait(delay);
  for (const name of names) {
    if (!still()) return;
    if (!has(name)) await fetch(name);
  }
}

// A failed read never drops what is kept: the old answer stays. A built answer
// (an object) carries `readAt`, the time it was read, whichever view built it.
export function settle(store, key, result, build, now = Date.now) {
  if (result?.ok) {
    const built = build(result.data);
    const value = built && typeof built === 'object' && !Array.isArray(built) ? { ...built, readAt: now() } : built;
    store.put(key, value);
    return value;
  }
  return store.get(key);
}

// A view that opens on a period the store holds draws it as it is: no ask. Any
// later change (a return to Book, Try again, a move to another period, an add)
// asks as before. `opening`: the first run since the view appeared; `forced`:
// the member or the page asked for a fresh answer; `held`: a fresh answer is kept.
export const drawsFromKept = ({ opening, forced, held }) => opening && !forced && held;

// Asks one after another while `still()`: the server has one small core, so
// asks sent together finish no sooner than asks in turn (sandbox, 2026-10-21:
// two together 1113 and 1144 ms, one alone 217-331 ms). Returns what `run`
// gave for each ask it made.
export async function inTurn(items, run, still = () => true) {
  const done = [];
  for (const item of items) {
    if (!still()) break;
    done.push(await run(item));
  }
  return done;
}
