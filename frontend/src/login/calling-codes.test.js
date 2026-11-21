// SPDX-License-Identifier: AGPL-3.0-only
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { getCountries, getCountryCallingCode } from 'libphonenumber-js';
import callingCodes from './calling-codes.js';

test('the calling codes equal the package they were generated from', () => {
  const fromPackage = Object.fromEntries(getCountries().map((r) => [r, getCountryCallingCode(r)]));
  assert.deepEqual(callingCodes, fromPackage);
});
