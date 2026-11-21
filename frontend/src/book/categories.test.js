// SPDX-License-Identifier: AGPL-3.0-only
// The booking category's switch: the icon keys, and what the page does with a
// key it does not know (no icon).
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { CATEGORY_ICON_KEYS, categoryIcon, categoryTabId, CALENDAR_ID } from './categories.js';

test('a known key is its own icon, blank and an unknown key are none', () => {
  for (const key of CATEGORY_ICON_KEYS) assert.equal(categoryIcon(key), key);
  for (const key of ['', undefined, null, 'star', 'Person', 'person ']) assert.equal(categoryIcon(key), null);
});

test('every key has an icon in the switch, and only those', () => {
  const source = readFileSync(new URL('./FilterControls.jsx', import.meta.url), 'utf8');
  const map = /const ICONS = \{([^}]*)\}/.exec(source)[1];
  const keys = [...map.matchAll(/(\w+):\s*\w+Rounded/g)].map((found) => found[1]);
  assert.deepEqual(keys, CATEGORY_ICON_KEYS);
});

test('the tabs name the calendar region they control and each has its own id', () => {
  assert.equal(categoryTabId(7), 'booking-as-7');
  assert.notEqual(categoryTabId(7), categoryTabId(8));
  assert.ok(CALENDAR_ID);
  const source = readFileSync(new URL('./MemberShell.jsx', import.meta.url), 'utf8');
  assert.match(source, /role: 'tabpanel', id: CALENDAR_ID, 'aria-labelledby': categoryTabId\(filter\.category\)/);
});

test('the category is no longer chosen in the Filter', () => {
  const source = readFileSync(new URL('./FilterControls.jsx', import.meta.url), 'utf8');
  const sheet = source.slice(source.indexOf('export function FilterSheet'));
  const bar = source.slice(source.indexOf('export function FilterBar'), source.indexOf('// The icon of each key'));
  assert.doesNotMatch(sheet, /categor/);
  assert.doesNotMatch(bar, /categor/);
});
