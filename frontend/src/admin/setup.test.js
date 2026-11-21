// SPDX-License-Identifier: AGPL-3.0-only
import test from 'node:test';
import assert from 'node:assert/strict';
import {
  CATEGORIES, TYPES, categoryNames, emptyRole, iconWords, itemBody, nameOk, nameTaken, roleBody, roleRefusals, setupFailure, ticked,
  usedWords,
} from './setup.js';

const roles = [
  { id: 1, name: 'Standard', categories: [10], admin_access: false, standard: true, members: 4 },
  { id: 2, name: 'Admin', categories: [10, 11], admin_access: true, standard: false, members: 1 },
];

test('a name is one to two hundred trimmed characters, taken whatever its case', () => {
  assert.equal(nameOk(' x '), true);
  assert.equal(nameOk('   '), false);
  assert.equal(nameOk('x'.repeat(200)), true);
  assert.equal(nameOk('x'.repeat(201)), false);
  assert.equal(nameTaken(roles, ' standard '), true);
  assert.equal(nameTaken(roles, 'standard', 1), false);
  assert.equal(nameTaken(roles, ''), false);
  assert.equal(nameTaken(roles, 'Guests'), false);
});

test('a change sends only what differs, a new row what it has', () => {
  const type = { id: 3, name: 'Sauna', description: 'Hot' };
  assert.deepEqual(itemBody(TYPES, type, { name: 'Sauna ', description: 'Hot', icon: '' }), {});
  assert.deepEqual(itemBody(TYPES, type, { name: 'Bath', description: 'Hot', icon: '' }), { name: 'Bath' });
  assert.deepEqual(itemBody(TYPES, type, { name: 'Sauna', description: '', icon: '' }), { description: '' });
  assert.deepEqual(itemBody(TYPES, null, { name: ' Boat ', description: '', icon: '' }), { name: 'Boat' });
  const category = { id: 4, name: 'Work', icon: 'work' };
  assert.deepEqual(itemBody(CATEGORIES, category, { name: 'Work', description: '', icon: '' }), { icon: '' });
  assert.deepEqual(itemBody(CATEGORIES, null, { name: 'Guest', description: '', icon: 'home' }), { name: 'Guest', icon: 'home' });
  assert.deepEqual(itemBody(CATEGORIES, null, { name: 'Guest', description: 'x', icon: '' }), { name: 'Guest' });
});

test('what holds a row is said in counts', () => {
  assert.equal(usedWords({ containers: 2, roles: 1, prices: 0, bookings: 3 }), 'In use: 2 containers, 1 role, 3 bookings.');
  assert.equal(usedWords({}), 'It is in use.');
  assert.equal(usedWords(undefined), 'It is in use.');
});

test('the icons have words and a blank is none', () => {
  assert.equal(iconWords(''), 'None');
  assert.equal(iconWords('group'), 'Group');
  assert.equal(iconWords('newer'), 'newer');
});

test('a failed call says why in plain words', () => {
  assert.match(setupFailure({ kind: 'offline' }), /could not be reached/);
  assert.match(setupFailure({ kind: 'forbidden' }), /out of date/);
  assert.equal(setupFailure({ kind: 'refused', code: 'in-use', body: { used: { roles: 2 } } }), 'In use: 2 roles.');
  assert.equal(setupFailure({ kind: 'refused', code: 'standard-admin' }), 'The standard role gives no admin access.');
  assert.equal(setupFailure({ kind: 'refused', code: 'last-administrator' }), 'One role keeps admin access.');
  assert.equal(setupFailure({ kind: 'refused', code: 'name-taken' }), 'Taken');
  assert.equal(setupFailure({ kind: 'refused', code: 'nothing-known' }), 'The change could not be made.');
});

test('a tick keeps the categories in the list order', () => {
  assert.deepEqual(ticked([10, 11, 12], [12], 10), [10, 12]);
  assert.deepEqual(ticked([10, 11, 12], [10, 12], 12), [10]);
  assert.deepEqual(ticked([10, 11, 12], [], 11), [11]);
});

test('a role as it would be saved is refused for the drawing\'s three reasons and for no category', () => {
  const standard = roles[0];
  assert.deepEqual(roleRefusals(roles, { ...standard }), []);
  assert.deepEqual(roleRefusals(roles, { ...standard, admin_access: true }), ['The standard role gives no admin access.']);
  assert.deepEqual(roleRefusals(roles, { ...roles[1], admin_access: false }), ['One role keeps admin access.']);
  assert.deepEqual(roleRefusals(roles, { ...standard, standard: false }), ['One role stays the standard role.']);
  assert.deepEqual(roleRefusals(roles, { ...standard, categories: [] }), ['A role needs a booking category.']);
  // A new role made standard takes the status from the old one.
  assert.deepEqual(roleRefusals(roles, { ...emptyRole(), name: 'Guests', categories: [10], standard: true }), []);
  assert.deepEqual(roleRefusals(roles, { ...emptyRole(), name: 'Guests', categories: [10] }), []);
});

test('a role sends all four when new and what differs when changed', () => {
  assert.deepEqual(roleBody(null, { ...emptyRole(), name: ' Guests ', categories: [10] }),
    { name: 'Guests', categories: [10], admin_access: false, standard: false });
  assert.equal(roleBody(roles[0], { ...roles[0], name: 'Standard ' }), null);
  assert.deepEqual(roleBody(roles[0], { ...roles[0], categories: [10, 11] }), { categories: [10, 11] });
  assert.deepEqual(roleBody(roles[1], { ...roles[1], name: 'Committee', admin_access: false }), { name: 'Committee', admin_access: false });
});

test('a roles categories are named in the categories\' order', () => {
  const categories = [{ id: 10, name: 'Private' }, { id: 11, name: 'Work' }];
  assert.equal(categoryNames(roles[1], categories), 'Private, Work');
  assert.equal(categoryNames({ categories: [] }, categories), '–');
});
