// SPDX-License-Identifier: AGPL-3.0-only
import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  PILL_BOTTOM, PILL_GAP, pillStrip, ROOM_LEAST, ROW_LEAST, ROW_MOST, rowHeight, scrollStart, sideBySide, weekRoom,
} from './fit.js';

const day = { from: 8 * 60, to: 20 * 60 }; // 12 hours

test('the row fills the room between the least and the largest', () => {
  assert.equal(rowHeight(300, day), ROW_LEAST);
  assert.equal(rowHeight(12 * 50, day), 50);
  assert.equal(rowHeight(12 * 50 + 11, day), 50);
  assert.equal(rowHeight(5000, day), ROW_MOST);
  assert.equal(rowHeight(Number.NaN, day), ROW_LEAST);
});

test('the Week gets the window below its top, never less than the least', () => {
  assert.equal(weekRoom(900, 300), 584);
  assert.equal(weekRoom(690, 300), 374);
  assert.equal(weekRoom(560, 300), ROOM_LEAST);
  assert.equal(weekRoom(600, 300), 284);
  assert.equal(weekRoom(300, 400), ROOM_LEAST);
});

test('the pill keeps a strip: its distance from the bottom, its height and a gap; none while hidden', () => {
  assert.equal(pillStrip(0), 0);
  assert.equal(pillStrip(52), PILL_BOTTOM + 52 + PILL_GAP);
  assert.equal(pillStrip(52), 84);
});

test('the Week ends above the pill strip when that is more than the page padding', () => {
  assert.equal(weekRoom(900, 300, 0), 584);
  assert.equal(weekRoom(900, 300, 84), 516);
  // A strip smaller than the padding changes nothing.
  assert.equal(weekRoom(900, 300, 10), 584);
  // The pill's bottom edge (window - 24 - 52) is never under the Week: it ends at 300 + 516 = 816 <= 900 - 76.
  assert.ok(300 + weekRoom(900, 300, pillStrip(52)) <= 900 - PILL_BOTTOM - 52);
  assert.equal(weekRoom(600, 300, 84), ROOM_LEAST);
});

test('the hours open at the first open hour; on today, an hour before now', () => {
  const view = 300;
  assert.equal(scrollStart({ hours: day, row: 34, now: null, view }), 0);
  assert.equal(scrollStart({ hours: day, row: 34, now: 7 * 60, view }), 0);
  assert.equal(scrollStart({ hours: day, row: 34, now: 8 * 60 + 30, view }), 0);
  assert.equal(scrollStart({ hours: day, row: 34, now: 11 * 60 + 20, view }), 2 * 34);
  // Never past the end: 12 * 34 = 408, 108 to scroll.
  assert.equal(scrollStart({ hours: day, row: 34, now: 19 * 60, view }), 108);
  // Everything fits: no scroll.
  assert.equal(scrollStart({ hours: day, row: 34, now: 14 * 60, view: 500 }), 0);
});

test('the Month sits beside its panel from 900 px', () => {
  assert.equal(sideBySide(899), false);
  assert.equal(sideBySide(900), true);
  assert.equal(sideBySide(640), false);
});
