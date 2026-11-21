// SPDX-License-Identifier: AGPL-3.0-only
import test from 'node:test';
import assert from 'node:assert/strict';
import {
  BLOCK_WORDS, CHANGE_WORDS, addBody, addReady, addTitle, addressWords, blockFailure, changeFailure, codeBody, correctBody, correctReady, correctTitle, emailBody,
  emailReady, emailTitle, exportFileName, exportTitle, blockTitle, blockToast, firstDirection, lastLoginWords, memberBookingsQuery,
  membersQuery, opensOnKey, canResetAuthenticator, originWords, phoneWords, postalWords, resetTitle, sortedBy, startRole,
} from './members.js';

const open = { status: 'all', role: 'all', q: '', sort: 'name', dir: 'asc', page: 0 };

test('no filter asks for the names ascending, page 1', () => {
  assert.deepEqual(membersQuery(open), {
    status: undefined, role: undefined, q: undefined, sort: 'name', order: 'asc', page: 1,
  });
});

test('a status, a role, a trimmed search, the last login newest first, page 3', () => {
  assert.deepEqual(membersQuery({ status: 'blocked', role: 7, q: '  Elm 4 ', sort: 'login', dir: 'desc', page: 2 }), {
    status: 'blocked', role: 7, q: 'Elm 4', sort: 'login', order: 'desc', page: 3,
  });
  assert.equal(membersQuery({ ...open, q: '   ' }).q, undefined);
});

test('a header starts in its own direction and a second click turns it', () => {
  assert.equal(firstDirection('name'), 'asc');
  assert.equal(firstDirection('unit'), 'asc');
  assert.equal(firstDirection('login'), 'desc');
  assert.deepEqual(sortedBy({ sort: 'name', dir: 'asc' }, 'login'), { sort: 'login', dir: 'desc' });
  assert.deepEqual(sortedBy({ sort: 'login', dir: 'desc' }, 'login'), { sort: 'login', dir: 'asc' });
  assert.deepEqual(sortedBy({ sort: 'login', dir: 'asc' }, 'unit'), { sort: 'unit', dir: 'asc' });
  assert.deepEqual(sortedBy({ sort: 'name', dir: 'asc' }, 'name'), { sort: 'name', dir: 'desc' });
});

test('a member\'s bookings are the Bookings\' pages with the member', () => {
  assert.deepEqual(memberBookingsQuery(12, 0), { member: 12, sort: 'number', order: 'newest', page: 1 });
  assert.equal(memberBookingsQuery(12, 2).page, 3);
});

test('a Norwegian number is grouped, another is shown as kept', () => {
  assert.equal(phoneWords('+4791234567'), '+47 912 34 567');
  assert.equal(phoneWords('+14155550123'), '+14155550123');
  assert.equal(phoneWords(''), '');
});

test('an address is the street and the membership\'s unit, a postal code the code and the city', () => {
  assert.equal(addressWords({ address: '2 Elm Street', flat: 'H0201' }), '2 Elm Street, H0201');
  assert.equal(addressWords({ address: '2 Elm Street', flat: '' }), '2 Elm Street');
  assert.equal(addressWords({ address: '', flat: 'H0201' }), 'H0201');
  assert.doesNotMatch(addressWords({ address: '2 Elm Street', flat: 'H0201' }), /flat/);
  assert.equal(addressWords({ address: '', flat: '' }), '–');
  assert.equal(postalWords({ postal_code: '4321', city: 'Northbridge' }), '4321 Northbridge');
  assert.equal(postalWords({ postal_code: '4321', city: '' }), '4321');
  assert.equal(postalWords({ postal_code: '', city: '' }), '–');
});

test('where a member came from: the role, the import or the operator, the date', () => {
  const at = '2027-01-10, 09:00:00';
  assert.equal(originWords({ role: 'Standard', origin: { kind: 'import', import: 3, at } }),
    'Standard · from member import 3, 2027-01-10, 09:00:00');
  assert.equal(originWords({ role: 'Standard', origin: { kind: 'import', import: null, at } }),
    'Standard · from a member import, 2027-01-10, 09:00:00');
  assert.equal(originWords({ role: 'Board', origin: { kind: 'operator', import: null, at: null } }), 'Board · set up by the operator');
  assert.equal(originWords({ role: 'Board', origin: { kind: null, import: null, at: null } }), 'Board');
});

test('no login reads Never', () => {
  assert.equal(lastLoginWords(null), 'Never');
  assert.equal(lastLoginWords('2027-01-10, 09:00:00'), '2027-01-10, 09:00:00');
});

test('the sheet asks Block or Unblock by the first name, and the snackbar says what was done', () => {
  assert.equal(blockTitle({ status: 'active', first_name: 'Alex', name: 'Alex Rivers' }), 'Block Alex?');
  assert.equal(blockTitle({ status: 'blocked', first_name: 'Alex', name: 'Alex Rivers' }), 'Unblock Alex?');
  assert.equal(blockTitle({ status: 'disabled', first_name: '', name: 'Member 14' }), 'Block Member 14?');
  assert.equal(blockToast({ status: 'blocked' }), 'Unblocked.');
  assert.equal(blockToast({ status: 'active' }), 'Blocked.');
});

test('a failed Block says why in plain words', () => {
  for (const code of Object.keys(BLOCK_WORDS)) {
    assert.equal(blockFailure({ kind: 'refused', code }), BLOCK_WORDS[code]);
  }
  assert.match(blockFailure({ kind: 'offline' }), /could not be reached/);
  assert.match(blockFailure({ kind: 'forbidden' }), /out of date/);
  assert.equal(blockFailure({ kind: 'refused', code: 'other' }), 'The member could not be changed.');
  assert.equal(blockFailure({ kind: 'refused', code: null }), 'The member could not be changed.');
});

test('Enter on the row opens it; Enter in a control inside it, or another key, does not', () => {
  const row = {};
  assert.equal(opensOnKey({ key: 'Enter', target: row, currentTarget: row }), true);
  assert.equal(opensOnKey({ key: 'Enter', target: {}, currentTarget: row }), false);
  assert.equal(opensOnKey({ key: ' ', target: row, currentTarget: row }), false);
  assert.equal(opensOnKey({ key: 'Tab', target: row, currentTarget: row }), false);
});

const alex = { id: 7, name: 'Alex Rivers', first_name: 'Alex', phone: '+4790000001', role_id: 3, email: 'alex@harbourview.example' };

test('a correction sends only what differs, the phone without its spaces', () => {
  const same = { name: 'Alex Rivers', phone: '+47 900 00 001', role: 3, code: '123456' };
  assert.equal(correctBody(alex, same), null);
  assert.deepEqual(correctBody(alex, { ...same, name: ' Alex R ' }), { code: '123456', full_name: 'Alex R' });
  assert.deepEqual(correctBody(alex, { ...same, phone: '+47 900 00 099' }), { code: '123456', phone: '+47 900 00 099' });
  assert.deepEqual(correctBody(alex, { ...same, phone: '' }), { code: '123456', phone: '' });
  assert.deepEqual(correctBody(alex, { ...same, role: 4 }), { code: '123456', role: 4 });
  assert.equal(correctBody(alex, { ...same, name: '  ' }), null);
});

test('a sheet is ready with a code and something to send', () => {
  assert.equal(correctReady(null), false);
  assert.equal(correctReady({ code: ' ', role: 4 }), false);
  assert.equal(correctReady({ code: '123456', role: 4 }), true);
  assert.equal(emailReady(alex, 'new@harbourview.example', '123456'), true);
  assert.equal(emailReady(alex, 'new@harbourview.example', ''), false);
  assert.equal(emailReady(alex, '  ', '123456'), false);
  assert.equal(emailReady(alex, ' Alex@Harbourview.example ', '123456'), false);
  assert.deepEqual(emailBody(' a@b.example ', '123456'), { email: 'a@b.example', code: '123456' });
  assert.deepEqual(codeBody('123456'), { code: '123456' });
});

test('the sheets are titled by the member, and the export is member-<id>.json', () => {
  assert.equal(correctTitle(alex), 'Correct Alex Rivers');
  assert.equal(emailTitle(alex), 'Change Alex\'s email');
  assert.equal(exportTitle(alex), 'Export Alex Rivers\'s data');
  assert.equal(resetTitle(alex), 'Reset Alex\'s authenticator');
  assert.equal(exportFileName(7), 'member-7.json');
});

test('Reset authenticator shows only on another administrator\'s page', () => {
  assert.equal(canResetAuthenticator({ administrator: true, own: false }), true);
  assert.equal(canResetAuthenticator({ administrator: true, own: true }), false);
  assert.equal(canResetAuthenticator({ administrator: false, own: false }), false);
  assert.equal(canResetAuthenticator({ own: false }), false);
});

test('a failed Correct, Change email or Export says why in plain words', () => {
  for (const code of Object.keys(CHANGE_WORDS)) {
    assert.equal(changeFailure({ kind: 'refused', code }), CHANGE_WORDS[code]);
  }
  assert.match(changeFailure({ kind: 'refused', code: 'code-refused' }), /code was not accepted/);
  assert.match(changeFailure({ kind: 'offline' }), /could not be reached/);
  assert.match(changeFailure({ kind: 'forbidden' }), /out of date/);
  assert.equal(changeFailure({ kind: 'refused', code: 'other' }), 'The member could not be changed.');
});

test('no word of the Members says flat or unit', () => {
  const words = Object.values(CHANGE_WORDS).concat(Object.values(BLOCK_WORDS));
  for (const word of words) assert.doesNotMatch(word, /\b(flat|unit)\b/i);
});

test('the Add member sheet sends trimmed text, the phone with its country code (Norwegian when none), the role id and the code', () => {
  const fields = { name: ' Robin Tester ', email: ' robin@testers.example ', phone: '900 00 123', role: 4, code: '123 456' };
  assert.deepEqual(addBody(fields), {
    full_name: 'Robin Tester', email: 'robin@testers.example', phone: '+4790000123', role: 4, code: '123 456',
  });
  assert.equal(addBody({ ...fields, phone: '+46 70 123 45 67' }).phone, '+46701234567');
  assert.equal(addBody({ ...fields, phone: '' }).phone, '');
  assert.equal(addTitle, 'Add member');
});

test('Add member can be sent with a name, an email, a role and a code; the phone is optional', () => {
  const ready = { name: 'Robin', email: 'r@t.example', phone: '', role: 4, code: '123456' };
  assert.equal(addReady(ready), true);
  for (const missing of [{ name: '  ' }, { email: '' }, { role: '' }, { role: null }, { code: ' ' }]) {
    assert.equal(addReady({ ...ready, ...missing }), false);
  }
});

test('the sheet starts on the standard role when the list has it', () => {
  const roles = [{ value: 1, label: 'Board' }, { value: 2, label: 'Standard' }];
  assert.equal(startRole({ roles, standard_role: 2 }), 2);
  assert.equal(startRole({ roles, standard_role: 9 }), '');
  assert.equal(startRole({ roles, standard_role: null }), '');
  assert.equal(startRole(undefined), '');
});

test('a member an administrator added says so under the name', () => {
  const at = '22 Oct 2026 10:00';
  assert.equal(originWords({ role: 'Standard', origin: { kind: 'administrator', import: null, at } }),
    'Standard · added by an administrator, 22 Oct 2026 10:00');
});

test('every refusal of Add member has its own words', () => {
  for (const code of ['invalid-name', 'invalid-email', 'email-taken', 'invalid-phone', 'phone-taken', 'unknown-role', 'code-refused']) {
    assert.ok(CHANGE_WORDS[code], code);
  }
});
