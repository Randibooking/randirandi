// SPDX-License-Identifier: AGPL-3.0-only
import callingCodes from './calling-codes.js';

// Calling codes: calling-codes.js, generated from libphonenumber-js.
// Country names: the browser's Intl.DisplayNames, in English.
const names = new Intl.DisplayNames(['en'], { type: 'region' });

// Every country with its calling code, Norway first, the rest by name.
export const COUNTRIES = Object.entries(callingCodes)
  .map(([region, code]) => ({ region, code: `+${code}`, name: names.of(region) }))
  .filter((country) => country.name && country.name !== country.region)
  .sort((a, b) => (a.region === 'NO' ? -1 : b.region === 'NO' ? 1 : a.name.localeCompare(b.name, 'en')));
export const NORWAY = COUNTRIES[0];

export const countryLabel = (country) => `${country.name} ${country.code}`;

// The member import's rule (member_layout.py _phone): drop spaces and -.()[],
// "00" becomes "+", a number that starts with "+" is kept, otherwise the code
// and the number without one leading 0.
export function joinPhone(code, number) {
  let value = [...number].filter((c) => !' -.()[]'.includes(c)).join('');
  if (value.startsWith('00')) value = `+${value.slice(2)}`;
  if (!value || value.startsWith('+')) return value;
  return code + (value.startsWith('0') ? value.slice(1) : value);
}

// A shown-again value that starts with "+" opens on Phone: the longest calling
// code that fits, the main country of that code, the rest as the number.
export function splitPhone(value) {
  if (!value.startsWith('+')) return null;
  const digits = value.slice(1);
  for (let length = 3; length > 0; length -= 1) {
    // The file lists the main country of a shared code first; a country the
    // browser has no name for is not in the list, so the next one is taken.
    const country = Object.keys(callingCodes)
      .filter((r) => callingCodes[r] === digits.slice(0, length))
      .map((r) => COUNTRIES.find((c) => c.region === r))
      .find(Boolean);
    if (country) return { country, number: digits.slice(length) };
  }
  return null;
}
