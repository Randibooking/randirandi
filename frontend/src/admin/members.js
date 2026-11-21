// SPDX-License-Identifier: AGPL-3.0-only
// The Members place and a member's page: their pure logic, no DOM. The filters
// as the server's query (randirandi/core/admin_views.py), the words of a phone,
// an address and where a member came from, and what a refused Block says.

import { NORWAY, joinPhone } from '../login/phone.js';

export const MEMBER_STATUS_COLORS = { active: 'success', disabled: 'default', blocked: 'error' };

// The list's query: `sort` is "name", "unit" or "login", `dir` "asc" or "desc";
// `page` counts from 0 here, from 1 there.
export function membersQuery({ status, role, q, sort, dir, page }) {
  return {
    status: status === 'all' ? undefined : status,
    role: role === 'all' ? undefined : role,
    q: q.trim() || undefined,
    sort,
    order: dir,
    page: page + 1,
  };
}

// A header's click: the same column turns, another starts in its own first
// direction (the last login newest first, the others ascending).
export const firstDirection = (sort) => (sort === 'login' ? 'desc' : 'asc');
export const sortedBy = (current, sort) => (current.sort === sort
  ? { sort, dir: current.dir === 'asc' ? 'desc' : 'asc' }
  : { sort, dir: firstDirection(sort) });

// One member's bookings, newest first, in the Bookings' pages.
export const memberBookingsQuery = (id, page) => ({ member: id, sort: 'number', order: 'newest', page: page + 1 });

// A Norwegian number as +47 912 34 567; any other is shown as it is kept.
export function phoneWords(phone) {
  const norway = /^\+47([0-9]{3})([0-9]{2})([0-9]{3})$/.exec(phone);
  return norway ? `+47 ${norway[1]} ${norway[2]} ${norway[3]}` : phone;
}

// A membership's address as the page words it: "2 Elm Street, H0201".
export function addressWords({ address, flat }) {
  const parts = [address, flat].filter(Boolean);
  return parts.join(', ') || '–';
}

export const postalWords = ({ postal_code: code, city }) => [code, city].filter(Boolean).join(' ') || '–';

// Where the member came from, under the name: the role, the import or the
// operator, and the date of the first status.
export function originWords(member) {
  const { kind, import: number, at } = member.origin;
  const from = kind === 'import' ? (number ? `from member import ${number}` : 'from a member import')
    : kind === 'operator' ? 'set up by the operator'
      : kind === 'administrator' ? 'added by an administrator' : '';
  const since = from && at ? `${from}, ${at}` : from || at || '';
  return [member.role, since].filter(Boolean).join(' · ');
}

export const lastLoginWords = (last) => last ?? 'Never';

// What the sheet before a Block or an Unblock asks and says.
export const blockTitle = (member) => `${member.status === 'blocked' ? 'Unblock' : 'Block'} ${member.first_name || member.name}?`;
export const blockToast = (member) => (member.status === 'blocked' ? 'Unblocked.' : 'Blocked.');

export const BLOCK_WORDS = {
  self: 'You cannot block yourself.',
  'already-blocked': 'The member is already blocked.',
  'not-blocked': 'The member is not blocked.',
  busy: 'Another change is being made. Try again in a moment.',
  'not-found': 'The member was not found.',
};
const FALLBACK = 'The member could not be changed.';

// What a failed call says, in plain words.
export function blockFailure(result) {
  if (result.kind === 'offline') return 'The server could not be reached. Nothing was changed.';
  if (result.kind === 'forbidden') return 'This page is out of date. Reload it and try again.';
  return BLOCK_WORDS[result.code] ?? FALLBACK;
}

// A row or card opens on Enter when the row itself has the focus; a key
// pressed in a control inside it is that control's.
export const opensOnKey = (event) => event.key === 'Enter' && event.target === event.currentTarget;

// Correct, Change email and Export data (part 7.92). Each sheet asks for the
// administrator's authenticator code; the server judges it.
export const correctTitle = (member) => `Correct ${member.name}`;
export const emailTitle = (member) => `Change ${member.first_name || member.name}'s email`;
export const exportTitle = (member) => `Export ${member.name}'s data`;
// The Reset authenticator button: another administrator's page only.
export const canResetAuthenticator = (member) => member.administrator === true && member.own !== true;
export const resetTitle = (member) => `Reset ${member.first_name || member.name}'s authenticator`;
export const exportFileName = (id) => `member-${id}.json`;

const noSpaces = (text) => text.replace(/\s+/g, '');

// What the correction sheet sends: only what differs from the member as the
// page shows them (the phone is compared without spaces); null when nothing
// does. The role is the chosen role's id.
export function correctBody(member, { name, phone, role, code }) {
  const body = { code };
  if (name.trim() && name.trim() !== member.name) body.full_name = name.trim();
  if (noSpaces(phone) !== member.phone) body.phone = phone;
  if (role !== member.role_id) body.role = role;
  return Object.keys(body).length > 1 ? body : null;
}

// Add member (part 8.2): the sheet's fields as the server's body; `role` is a
// role's id, the phone may be empty. A number without a country code is a
// Norwegian one, as on the sign-in page (login/phone.js: joinPhone). Ready when the name, the email, a role and
// a code are given; the server judges each value.
export const addTitle = 'Add member';
export const addBody = ({ name, email, phone, role, code }) => ({
  full_name: name.trim(), email: email.trim(), phone: joinPhone(NORWAY.code, phone), role, code,
});
export const addReady = ({ name, email, role, code }) => name.trim() !== '' && email.trim() !== '' && role !== '' && role != null && code.trim() !== '';
// The role the sheet starts on: the organization's standard role when it is
// among the roles, else none.
export const startRole = (data) => (data?.roles ?? []).some((r) => r.value === data?.standard_role) ? data.standard_role : '';

export const emailBody = (email, code) => ({ email: email.trim(), code });
export const codeBody = (code) => ({ code });

// A sheet can be sent when it has a code, and a change or an address.
export const correctReady = (body) => body !== null && body.code.trim() !== '';
export const emailReady = (member, email, code) => email.trim() !== '' && email.trim().toLowerCase() !== member.email && code.trim() !== '';

export const CHANGE_WORDS = {
  'code-refused': 'The code was not accepted.',
  'code-limited': 'Too many codes were not accepted. Wait 15 minutes and try again.',
  'invalid-name': 'The name is not valid.',
  'invalid-phone': 'The phone number is not valid.',
  'phone-taken': 'Another member has this phone number.',
  'unknown-role': 'The role was not found.',
  'own-role': 'You cannot change your own role.',
  'last-administrator': 'The organization needs an administrator.',
  'invalid-email': 'The email is not valid.',
  'email-taken': 'Another member has this email.',
  unchanged: 'This is already the email.',
  blocked: 'Unblock the member first.',
  erased: 'The member is erased.',
  self: 'You cannot reset your own authenticator.',
  'not-administrator': 'The member is not an administrator.',
  'bad-request': 'The request was not understood.',
  busy: 'Another change is being made. Try again in a moment.',
  'not-found': 'The member was not found.',
};

// What a failed Correct, Change email or Export says, in plain words.
export function changeFailure(result) {
  if (result.kind === 'offline') return 'The server could not be reached. Nothing was changed.';
  if (result.kind === 'forbidden') return 'This page is out of date. Reload it and try again.';
  return CHANGE_WORDS[result.code] ?? FALLBACK;
}
