// SPDX-License-Identifier: AGPL-3.0-only
import assert from 'node:assert/strict';
import test from 'node:test';
import { auditStep, targetAddress, targetMove } from './auditLinks.js';

test('a member and a member import open their own page', () => {
  assert.deepEqual(targetMove({ kind: 'member', id: 7 }), { type: 'member', id: 7 });
  assert.equal(targetAddress({ kind: 'member', id: 7 }), '/admin/members/7/');
  assert.deepEqual(targetMove({ kind: 'member_import', id: 3 }), { type: 'run', id: 3 });
  assert.equal(targetAddress({ kind: 'member_import', id: 3 }), '/admin/members/imports/3/');
});

test('a container opens in the Containers', () => {
  assert.deepEqual(targetMove({ kind: 'booking_configuration', id: 12 }), { type: 'container', id: 12 });
  assert.equal(targetAddress({ kind: 'booking_configuration', id: 12 }), '/admin/containers/');
});

test('the kinds with a place open it', () => {
  assert.deepEqual(targetMove({ kind: 'role', id: 2 }), { type: 'place', place: 'roles' });
  assert.equal(targetAddress({ kind: 'block', id: 2 }), '/admin/blocks/');
  assert.equal(targetAddress({ kind: 'spot', id: 2 }), '/admin/assets/');
  assert.equal(targetAddress({ kind: 'booking_category', id: 2 }), '/admin/types/');
  assert.equal(targetAddress({ kind: 'price_book_entry', id: 2 }), '/admin/prices/');
  assert.equal(targetAddress({ kind: 'organization', id: 1 }), '/admin/organization/');
});

test('no target, or a kind with no page, opens nothing', () => {
  assert.equal(targetMove(null), null);
  assert.equal(targetAddress(null), null);
  assert.equal(targetMove({ kind: 'login_code', id: 1 }), null);
});

test('an event opens a booking first, else its target, else nothing', () => {
  assert.deepEqual(auditStep({ booking: 5, target: null }), { type: 'booking', number: 5 });
  assert.deepEqual(auditStep({ booking: null, target: { kind: 'member', id: 7 } }), { type: 'member', id: 7 });
  assert.deepEqual(auditStep({ booking: null, target: { kind: 'member_import', id: 3 } }), { type: 'run', id: 3 });
  assert.deepEqual(auditStep({ booking: null, target: { kind: 'booking_configuration', id: 12 } }), { type: 'container', id: 12 });
  assert.deepEqual(auditStep({ booking: null, target: { kind: 'role', id: 2 } }), { type: 'place', place: 'roles' });
  assert.equal(auditStep({ booking: null, target: null }), null);
  assert.equal(auditStep({ booking: null, target: { kind: 'login_code', id: 1 } }), null);
});

test('an event opens its page, by its address', () => {
  assert.deepEqual(targetMove({ kind: 'event', id: 9 }), { type: 'event', id: 9 });
  assert.equal(targetAddress({ kind: 'event', id: 9 }), '/admin/events/9/');
  assert.deepEqual(auditStep({ booking: null, target: { kind: 'event', id: 9 } }), { type: 'event', id: 9 });
});

test('an event opens its page, by its address', () => {
  assert.deepEqual(targetMove({ kind: 'event', id: 9 }), { type: 'event', id: 9 });
  assert.equal(targetAddress({ kind: 'event', id: 9 }), '/admin/events/9/');
  assert.deepEqual(auditStep({ booking: null, target: { kind: 'event', id: 9 } }), { type: 'event', id: 9 });
});
