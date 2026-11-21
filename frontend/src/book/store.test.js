// SPDX-License-Identifier: AGPL-3.0-only
import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  KEPT, MAX_AGE, createStore, drawsFromKept, inTurn, keyOf, neighbours, prefetch, settle,
} from './store.js';

test('keyOf: the whole question; field order does not matter', () => {
  const a = keyOf({ asset: 1, category: 2 }, '2026-10-01', 31);
  assert.equal(a, keyOf({ category: 2, asset: 1 }, '2026-10-01', 31));
  assert.notEqual(a, keyOf({ asset: 1, category: 2 }, '2026-11-01', 30));
  assert.notEqual(a, keyOf({ asset: 1, category: 2 }, '2026-10-01', 7));
  assert.notEqual(a, keyOf({ asset: 1, category: 3 }, '2026-10-01', 31));
  assert.notEqual(a, keyOf({ asset: 2, category: 2 }, '2026-10-01', 31));
  assert.notEqual(a, keyOf({ asset: 1, category: 2, editing: 'x' }, '2026-10-01', 31));
  assert.equal(a, keyOf({ asset: 1, category: 2, group: null, type: undefined }, '2026-10-01', 31));
});

test('the store keeps at most a dozen, the least recently used going first', () => {
  const store = createStore();
  assert.equal(KEPT, 12);
  for (let i = 0; i < 12; i += 1) store.put(`k${i}`, i);
  assert.equal(store.size, 12);
  store.get('k0');
  store.put('k12', 12);
  assert.equal(store.size, 12);
  assert.equal(store.has('k1'), false);
  assert.equal(store.get('k0'), 0);
});

test('put again replaces the answer', () => {
  const store = createStore(2);
  store.put('a', 1);
  store.put('a', 2);
  assert.equal(store.get('a'), 2);
  assert.equal(store.size, 1);
});

test('clear drops everything', () => {
  const store = createStore();
  store.put('a', 1);
  store.clear();
  assert.equal(store.get('a'), undefined);
  assert.equal(store.size, 0);
});

test('settle: an answer replaces the old one; a failure keeps it', () => {
  const store = createStore();
  store.put('a', 'old');
  assert.equal(settle(store, 'a', { ok: true, data: 5 }, (data) => `new${data}`), 'new5');
  assert.equal(store.get('a'), 'new5');
  assert.equal(settle(store, 'a', { kind: 'offline' }, () => 'x'), 'new5');
  assert.equal(settle(store, 'b', { kind: 'refused' }, () => 'x'), undefined);
  assert.equal(store.has('b'), false);
});

test('an answer older than five minutes is not kept', () => {
  let time = 1000;
  const store = createStore(12, () => time);
  store.put('a', 1);
  time += MAX_AGE;
  assert.equal(store.get('a'), 1);
  time += 1;
  assert.equal(store.has('a'), false);
  assert.equal(store.get('a'), undefined);
  assert.equal(store.size, 0);
  // A failed read cannot bring it back.
  assert.equal(settle(store, 'a', { kind: 'offline' }, () => 'x'), undefined);
  store.put('a', 2);
  assert.equal(store.get('a'), 2);
});

const shift = (month, count) => `m${Number(month.slice(1)) + count}`;

test('neighbours: the direction of travel first; no past when it cannot be reached', () => {
  assert.deepEqual(neighbours('m5', 1, shift, true), ['m6', 'm4']);
  assert.deepEqual(neighbours('m5', -1, shift, true), ['m4', 'm6']);
  assert.deepEqual(neighbours('m5', -1, shift, false), ['m6']);
});

test('prefetch: after the delay, one at a time, in order, skipping what is kept', async () => {
  const log = [];
  let open = false;
  await prefetch({
    names: ['a', 'b', 'c'],
    has: (name) => name === 'b',
    fetch: async (name) => { log.push(`start ${name}`); await null; log.push(`end ${name}`); },
    still: () => true,
    wait: async (ms) => { log.push(`wait ${ms}`); open = true; },
  });
  assert.equal(open, true);
  assert.deepEqual(log, ['wait 300', 'start a', 'end a', 'start c', 'end c']);
});

test('prefetch: stops when the member has left the period', async () => {
  const log = [];
  let here = true;
  await prefetch({
    names: ['a', 'b'],
    has: () => false,
    fetch: async (name) => { log.push(name); here = false; },
    still: () => here,
    wait: async () => {},
  });
  assert.deepEqual(log, ['a']);
  const none = [];
  await prefetch({ names: ['a'], has: () => false, fetch: async (n) => none.push(n), still: () => false, wait: async () => {} });
  assert.deepEqual(none, []);
});

test('settle stamps a built answer with the time it was read, whoever built it', () => {
  const store = createStore();
  const built = settle(store, 'k', { ok: true, data: { n: 1 } }, (data) => ({ today: '2026-10-20', ...data }), () => 1234);
  assert.deepEqual(built, { today: '2026-10-20', n: 1, readAt: 1234 });
  assert.equal(store.get('k').readAt, 1234);
});

// --- One ask for a question, a switch asks nothing it holds (part 3.997) ------

const tick = () => new Promise((done) => { setTimeout(done, 0); });

test('ask: a second ask for a question on its way takes its answer; none is sent twice', async () => {
  const store = createStore();
  let sent = 0;
  let release;
  const start = () => new Promise((done) => { sent += 1; release = () => done({ ok: true, data: 'answer' }); });
  const first = store.ask('month', start);
  const second = store.ask('month', start);
  await tick();
  assert.equal(sent, 1);
  release();
  assert.deepEqual(await first, { ok: true, data: 'answer' });
  assert.equal(await second, await first);
  // Settled: the next ask is a new one.
  const third = store.ask('month', start);
  await tick();
  assert.equal(sent, 2);
  release();
  await third;
});

test('ask: another question is another ask; a refused answer is shared and then forgotten', async () => {
  const store = createStore();
  let sent = 0;
  const start = () => { sent += 1; return Promise.resolve({ kind: 'offline' }); };
  const both = await Promise.all([store.ask('a', start), store.ask('b', start), store.ask('a', start)]);
  assert.equal(sent, 2);
  assert.deepEqual(both[0], { kind: 'offline' });
  await store.ask('a', start);
  assert.equal(sent, 3);
});

test('clear: an ask on its way when the store is cleared is never handed on; the ask is sent again', async () => {
  const store = createStore();
  const releases = [];
  const start = () => new Promise((done) => { releases.push(done); });
  const asked = store.ask('month', start);
  await tick();
  store.clear();
  releases[0]({ ok: true, data: 'before the change' });
  await tick();
  // The old answer is not the result: a new ask is on its way.
  assert.equal(releases.length, 2);
  releases[1]({ ok: true, data: 'after the change' });
  assert.deepEqual(await asked, { ok: true, data: 'after the change' });
});

test('clear: the answer of an ask under an older generation is not put by settle; the page asks again', async () => {
  const store = createStore();
  const releases = [];
  const start = () => new Promise((done) => { releases.push(done); });
  const build = (data) => ({ data });
  const asked = store.ask('month', start).then((result) => settle(store, 'month', result, build));
  await tick();
  store.clear();
  releases[0]({ ok: true, data: 'old' });
  await tick();
  assert.equal(store.get('month'), undefined);
  releases[1]({ ok: true, data: 'new' });
  assert.equal((await asked).data, 'new');
  assert.equal(store.get('month').data, 'new');
});

test('clear: cleared again and again, the ask gives up with a stale answer and keeps nothing', async () => {
  const store = createStore();
  const releases = [];
  const start = () => new Promise((done) => { releases.push(done); });
  const asked = store.ask('month', start).then((result) => ({ result, built: settle(store, 'month', result, (data) => data) }));
  for (let i = 0; i < 3; i += 1) {
    await tick();
    store.clear();
    releases[i]({ ok: true, data: `old ${i}` });
  }
  const { result, built } = await asked;
  assert.deepEqual(result, { kind: 'stale' });
  assert.equal(built, undefined);
  assert.equal(store.size, 0);
});

test('ask: a start that rejects or throws answers offline, and the next month is still asked', async () => {
  const store = createStore();
  assert.deepEqual(await store.ask('a', () => Promise.reject(new Error('bad answer'))), { kind: 'offline' });
  assert.deepEqual(await store.ask('b', () => { throw new Error('bad'); }), { kind: 'offline' });
  const done = await inTurn(['a', 'b', 'c'], (key) => store.ask(key, () => (key === 'a' ? Promise.reject(new Error('x')) : { ok: true, data: key })));
  assert.deepEqual(done, [{ kind: 'offline' }, { ok: true, data: 'b' }, { ok: true, data: 'c' }]);
});

test('a view opening on a period the store holds draws it and asks nothing', () => {
  assert.equal(drawsFromKept({ opening: true, forced: false, held: true }), true);
  // Nothing held, a return to Book / Try again / an add, or a later move: ask.
  assert.equal(drawsFromKept({ opening: true, forced: false, held: false }), false);
  assert.equal(drawsFromKept({ opening: true, forced: true, held: true }), false);
  assert.equal(drawsFromKept({ opening: false, forced: false, held: true }), false);
});

test('inTurn: asks go one after another, and stop when the view has gone', async () => {
  const log = [];
  let open = 0;
  let most = 0;
  const run = async (item) => {
    open += 1;
    most = Math.max(most, open);
    log.push(item);
    await new Promise((done) => { setTimeout(done, 1); });
    open -= 1;
    return item * 2;
  };
  assert.deepEqual(await inTurn([1, 2, 3], run), [2, 4, 6]);
  assert.equal(most, 1);
  assert.deepEqual(await inTurn([], run), []);
  let live = true;
  const gone = await inTurn([7, 8, 9], async (item) => { live = false; return item; }, () => live);
  assert.deepEqual(gone, [7]);
});
