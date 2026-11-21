// SPDX-License-Identifier: AGPL-3.0-only
import test from 'node:test';
import assert from 'node:assert/strict';
import { BACK_TO_BOOKING, BOOKING_HREF, GROUPS, hrefOf, initials, labelOf, titleOf } from './places.js';

test('three groups, thirteen places, each with its own key', () => {
  assert.deepEqual(GROUPS.map(([group, places]) => [group, places.length]), [['Daily', 6], ['Setup', 6], ['Records', 1]]);
  const keys = GROUPS.flatMap(([, places]) => places.map(([key]) => key));
  assert.equal(new Set(keys).size, 13);
});

test('a place has its label and its address', () => {
  assert.equal(labelOf('assets'), 'Assets and availability');
  assert.equal(labelOf('nowhere'), '');
  assert.equal(hrefOf('audit'), '/admin/audit/');
});

test('initials come from the first and last word', () => {
  assert.equal(initials('Sam Field'), 'SF');
  assert.equal(initials('sam'), 'S');
  assert.equal(initials('Mary Ann Field'), 'MF');
  assert.equal(initials(''), '');
  assert.equal(initials(undefined), '');
});

test('the way back is one button to the booking pages, and the tab is titled as the server titles it', () => {
  assert.equal(BACK_TO_BOOKING, 'Back to Booking');
  assert.equal(BOOKING_HREF, '/book/');
  assert.equal(titleOf('members'), 'Members · Admin · RandiRandi');
});
