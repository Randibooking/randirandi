// SPDX-License-Identifier: AGPL-3.0-only
import test from 'node:test';
import assert from 'node:assert/strict';
import {
  EMAIL, daysWords, draftOf, eraseWords, failure, limitMonthsWords, limitWords, monthsWords, parseTax, parseWhole, readDraft,
  saveBody, taxText, taxWords,
} from './organization.js';

const place = {
  name: 'Harbourview co-op', subdomain: 'harbourview', contact_email: 'board@harbourview.example', free_cancellation_days: 5, standard_role: 1,
  max_bookings_a_day: 5, book_ahead_months: null, tax_rate: 2500, member_retention_months: 12, booking_retention_months: 36,
  language: 'en', time_zone: 'Europe/Oslo', currency: 'NOK', version: 'abc',
  roles: [{ id: 1, name: 'Standard', admin_access: false }, { id: 2, name: 'Board', admin_access: true }],
  languages: [{ code: 'en', name: 'English' }, { code: 'nb', name: 'Norsk bokmål' }],
  time_zones: ['Europe/Oslo', 'UTC'], currencies: ['NOK', 'DKK', 'EUR'],
};

test('the tax rate is told in percent from hundredths', () => {
  assert.equal(taxText(2500), '25');
  assert.equal(taxText(1250), '12.5');
  assert.equal(taxText(1205), '12.05');
  assert.equal(taxText(5), '0.05');
  assert.equal(taxText(0), '0');
  assert.equal(taxWords(2500), '25 %');
  assert.equal(taxWords(1250), '12.5 %');
});

test('a percent as typed is hundredths, or nothing', () => {
  assert.equal(parseTax('25'), 2500);
  assert.equal(parseTax(' 12,5 '), 1250);
  assert.equal(parseTax('0.05'), 5);
  assert.equal(parseTax('100'), 10000);
  assert.equal(parseTax('100.01'), null);
  assert.equal(parseTax('101'), null);
  assert.equal(parseTax('2.555'), null);
  assert.equal(parseTax('-1'), null);
  assert.equal(parseTax(''), null);
  assert.equal(parseTax('25 %'), null);
  for (const rate of [0, 1, 99, 100, 1250, 2500, 10000]) assert.equal(parseTax(taxText(rate)), rate);
});

test('whole numbers are from the least to the column\'s most', () => {
  assert.equal(parseWhole('12', 1), 12);
  assert.equal(parseWhole('0', 1), null);
  assert.equal(parseWhole('1.5', 1), null);
  assert.equal(parseWhole('-3', 1), null);
  assert.equal(parseWhole('32767', 1), 32767);
  assert.equal(parseWhole('32768', 1), null);
  assert.equal(parseWhole('', 1), null);
});

test('words for the summary', () => {
  assert.equal(monthsWords(1), '1 month');
  assert.equal(monthsWords(12), '12 months');
  assert.equal(limitWords(null), 'No limit');
  assert.equal(limitWords(5), '5');
  assert.equal(limitMonthsWords(6), '6 months');
  assert.equal(limitMonthsWords(null), 'No limit');
});

test('the sheet opens on the place and an untouched sheet saves nothing', () => {
  const draft = draftOf(place);
  assert.equal(draft.max_bookings_a_day, '5');
  assert.equal(draft.book_ahead_months, '');
  assert.equal(draft.tax_rate, '25');
  assert.equal(draft.standard_role, '1');
  assert.equal(saveBody(place, draft), null);
});

test('a save sends the version and only what differs', () => {
  const draft = { ...draftOf(place), name: ' New name ', max_bookings_a_day: '', book_ahead_months: '6', tax_rate: '12,5' };
  assert.deepEqual(saveBody(place, draft), {
    version: 'abc', name: 'New name', max_bookings_a_day: null, book_ahead_months: 6, tax_rate: 1250,
  });
});

test('a field that cannot be saved offers no save, with its words', () => {
  const bad = { ...draftOf(place), contact_email: 'nobody', member_retention_months: '0', time_zone: 'Mars/Base', tax_rate: '101' };
  const { faults } = readDraft(place, bad);
  assert.deepEqual(Object.keys(faults).sort(), ['contact_email', 'member_retention_months', 'tax_rate', 'time_zone']);
  assert.equal(saveBody(place, bad), null);
  assert.equal(readDraft(place, { ...draftOf(place), standard_role: '2' }).faults.standard_role, 'Choose a role.');
  assert.equal(readDraft(place, { ...draftOf(place), name: '  ' }).faults.name, 'Give it a name of up to 200 characters.');
  assert.equal(readDraft(place, { ...draftOf(place), name: 'x'.repeat(201) }).faults.name !== undefined, true);
  assert.equal(readDraft(place, { ...draftOf(place), max_bookings_a_day: '0' }).faults.max_bookings_a_day !== undefined, true);
});

test('the language is one the code ships', () => {
  assert.equal(draftOf(place).language, 'en');
  assert.deepEqual(saveBody(place, { ...draftOf(place), language: 'nb' }), { version: 'abc', language: 'nb' });
  assert.equal(readDraft(place, { ...draftOf(place), language: 'de' }).faults.language, 'Choose a language.');
  assert.equal(readDraft(place, { ...draftOf(place), language: '' }).faults.language, 'Choose a language.');
  assert.equal(failure({ kind: 'refused', code: 'invalid-language', body: { reason: 'must be a language the code ships: en, nb.' } }).text,
    'Must be a language the code ships: en, nb.');
});

test('a refusal belongs to its field, with the server\'s reason', () => {
  assert.deepEqual(
    failure({ kind: 'refused', code: 'invalid-contact-email', body: { reason: 'must be an email address.' } }),
    { field: 'contact_email', text: 'Must be an email address.' },
  );
  assert.deepEqual(
    failure({ kind: 'refused', code: 'invalid-max-bookings-a-day', body: { reason: 'must be 1 or more.' } }),
    { field: 'max_bookings_a_day', text: 'Must be 1 or more.' },
  );
  assert.equal(failure({ kind: 'refused', code: 'invalid-tax-rate', body: {} }).field, 'tax_rate');
  assert.deepEqual(failure({ kind: 'refused', code: 'standard-admin', body: {} }), { field: null, text: 'The standard role gives no admin access.' });
  assert.equal(failure({ kind: 'refused', code: 'stale', body: {} }).text, 'Someone else changed the details.');
  assert.equal(failure({ kind: 'refused', code: 'invalid-nothing', body: {} }).field, null);
  assert.equal(failure({ kind: 'offline' }).field, null);
  assert.match(failure({ kind: 'forbidden' }).text, /out of date/);
  assert.equal(failure({ kind: 'refused', code: 'weird', body: {} }).text, 'The change could not be made.');
});

test('a refused code says so', () => {
  assert.equal(failure({ kind: 'refused', code: 'code-refused', body: {} }).text, 'The code was not accepted.');
  assert.match(failure({ kind: 'refused', code: 'code-limited', body: {} }).text, /Wait 15 minutes/);
});

test('the confirmation says the numbers the server counted', () => {
  assert.equal(eraseWords({ members: 1, bookings: 2 }), '1 member and 2 bookings will be erased at the next run.');
  assert.equal(eraseWords({ members: 0, bookings: 0 }), '0 members and 0 bookings will be erased at the next run.');
});

test('the contact address is one plain address: no name, no list, no line break', () => {
  for (const good of ['desk@example.org', 'post+bookings@sub.example.org']) assert.ok(EMAIL.test(good), good);
  for (const bad of ['', 'nobody', 'a@b', 'Board <a@example.org>', '"Board" <a@example.org>', 'a@example.org, b@example.org',
    'a@example.org; b@example.org', 'a@example.org b@example.org', 'a@example.org\n', 'a@example.org\r\nBcc: x@example.org']) {
    assert.ok(!EMAIL.test(bad), JSON.stringify(bad));
  }
  const faults = (contact) => readDraft(place, { ...draftOf(place), contact_email: contact }).faults;
  assert.deepEqual(Object.keys(faults('Board <a@example.org>')), ['contact_email']);
  assert.equal(faults('Board <a@example.org>').contact_email, 'Enter one email address, without a name.');
  assert.deepEqual(faults(' desk@example.org '), {});
});

test('the days of free cancellation are a whole number from 0 to 365', () => {
  assert.equal(draftOf(place).free_cancellation_days, '5');
  const read = (days) => readDraft(place, { ...draftOf(place), free_cancellation_days: days });
  for (const [typed, days] of [['0', 0], ['365', 365], [' 7 ', 7]]) assert.equal(read(typed).values.free_cancellation_days, days);
  for (const bad of ['-1', '366', '1.5', '1,5', '', 'five', '5 days']) {
    assert.equal(read(bad).faults.free_cancellation_days, 'A whole number of days, from 0 to 365.', bad);
  }
  assert.equal(daysWords(1), '1 day');
  assert.equal(daysWords(0), '0 days');
  assert.equal(daysWords(5), '5 days');
});

test('a save sends the days and the address only when they changed', () => {
  assert.equal(saveBody(place, draftOf(place)), null);
  assert.deepEqual(saveBody(place, { ...draftOf(place), free_cancellation_days: '0' }), { version: 'abc', free_cancellation_days: 0 });
  assert.deepEqual(saveBody(place, { ...draftOf(place), free_cancellation_days: '9', contact_email: 'desk@example.org' }),
    { version: 'abc', contact_email: 'desk@example.org', free_cancellation_days: 9 });
  assert.equal(saveBody(place, { ...draftOf(place), free_cancellation_days: '366' }), null);
});

test('the server\'s refusal of the days is said in its own words', () => {
  assert.deepEqual(failure({ code: 'invalid-free-cancellation-days', body: { reason: 'must be 365 or less.' } }),
    { field: 'free_cancellation_days', text: 'Must be 365 or less.' });
});
