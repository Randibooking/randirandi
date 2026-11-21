// SPDX-License-Identifier: AGPL-3.0-only
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { COUNTRIES, NORWAY, joinPhone, splitPhone } from './phone.js';

test('joins a number to its code as the member import does', () => {
  assert.equal(joinPhone('+47', '912 34 567'), '+4791234567');
  assert.equal(joinPhone('+47', '0912-34-567'), '+4791234567');
  assert.equal(joinPhone('+44', '(0)7700 900123'), '+447700900123');
  assert.equal(joinPhone('+44', '07700 900123'), '+447700900123');
  assert.equal(joinPhone('+47', '0046 70 123 45 67'), '+46701234567');
  assert.equal(joinPhone('+47', '+44 7700 900123'), '+447700900123');
  assert.equal(joinPhone('+47', '  '), '');
  assert.equal(joinPhone('+47', '00'), '+');
});

test('lists Norway first and every country with a code', () => {
  assert.equal(NORWAY.region, 'NO');
  assert.equal(COUNTRIES[0], NORWAY);
  assert.ok(COUNTRIES.length > 200);
  assert.ok(COUNTRIES.every((c) => /^\+\d{1,3}$/.test(c.code) && c.name !== c.region));
  assert.ok(COUNTRIES.some((c) => c.name === 'Germany' && c.code === '+49'));
});

test('reopens a stored number on Phone with the longest matching code', () => {
  assert.deepEqual(splitPhone('+4791234567'), { country: NORWAY, number: '91234567' });
  assert.equal(splitPhone('+447700900123').country.region, 'GB');
  assert.equal(splitPhone('+447700900123').number, '7700900123');
  assert.equal(splitPhone('+18765550123').country.code, '+1');
  assert.equal(splitPhone('+3538712345').country.region, 'IE');
  assert.equal(splitPhone('alex@example.com'), null);
  assert.equal(splitPhone('+0123456'), null);
});
