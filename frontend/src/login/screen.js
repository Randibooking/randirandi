// SPDX-License-Identifier: AGPL-3.0-only
import { label, labelParts } from '../book/labels.js';
import { NORWAY, splitPhone } from './phone.js';

// The sign-in card has two screens: Email (the first) and the mobile number
// (reached by "What's my login?"). A remembered value that starts
// with "+" opens on the number; anything else opens on Email.
export function openOn(initialValue) {
  const reopened = splitPhone(initialValue);
  return reopened
    ? { screen: 'phone', email: '', country: reopened.country, number: reopened.number }
    : { screen: 'email', email: initialValue, country: NORWAY, number: '' };
}

export const SCREEN_AFTER = { email: 'phone', phone: 'email' };

// The button on each screen that turns the card to the other.
export const switchLabel = (screen) => label(screen === 'email' ? 'login.what-login' : 'login.with-email');

// The words under the number: the code goes by email to a registered member;
// anyone else writes to the organization's contact address (a `mailto:` link
// in the page). Returned in parts so the page can make the address a link.
export function numberWords(contactEmail) {
  const here = {};
  const parts = labelParts('login.help-number', { address: here });
  const at = parts.indexOf(here);
  return {
    before: (at < 0 ? parts : parts.slice(0, at)).join(''),
    address: contactEmail,
    href: `mailto:${contactEmail}`,
    after: at < 0 ? '' : parts.slice(at + 1).join(''),
  };
}
