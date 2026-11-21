// SPDX-License-Identifier: AGPL-3.0-only
// The Members place's Imports tab and a run's page: their pure logic, no DOM.
// The numbers and codes are the server's answer (randirandi/core/member_imports.py).

export const OUTCOME_COLORS = { applied: 'success', stopped: 'warning', failed: 'error' };
export const COUNT_WORDS = ['New', 'Changed', 'Active again', 'Disabled', 'Skipped'];

// The runs' query: `page` counts from 0 here, from 1 there.
export const importsQuery = (page) => ({ page: page + 1 });

// The words of a run's reason code, for the alert on a failed or stopped run.
const FAILED_WORDS = {
  invalid_xlsx: 'The file is not an Excel file the parser can read.',
  file_too_large: 'The file is larger than the parser reads.',
  xlsx_too_large: 'The file holds more than the parser reads.',
  not_one_sheet: 'The workbook has more than one sheet; the parser reads a file with one.',
  external_reference: 'The workbook refers to another file.',
  invalid_columns: 'The file is not in the layout the parser reads.',
  invalid_row_width: 'The file is not in the layout the parser reads.',
  invalid_layout: 'The file is not in the layout the parser reads.',
  invalid_column_map: 'The file is not in the layout the parser reads.',
  invalid_csv: 'The file is not in the layout the parser reads.',
  write_conflict: 'The members could not be written.',
};
const STOPPED_WORDS = {
  empty_list: 'The file has nobody on it.',
  standard_role_must_be_non_admin: 'The organization\'s standard role is missing or gives admin access.',
};
const NOTHING = 'Nothing was changed.';

// The alert on a run's page, or '' when the run has none. A stop by the safety
// limit says how many of the active members the list would have disabled.
export function noteWords(run) {
  if (run.outcome === 'applied') return '';
  const { reason } = run;
  if (run.outcome === 'failed' && reason === 'missing_column') {
    const column = run.column ? ` "${run.column}"` : '';
    return `The parser found no column${column} in the file; the file is not in the layout it reads. ${NOTHING}`;
  }
  if (run.outcome === 'stopped' && reason === 'revocation_limit') {
    const percent = run.active ? Math.round((100 * run.proposed) / run.active) : 0;
    return `${run.proposed} of the ${run.active} active members would have been disabled (${percent} %; the limit is 20 %). `
      + `A list that short is most likely not the whole list. ${NOTHING}`;
  }
  const words = (run.outcome === 'failed' ? FAILED_WORDS : STOPPED_WORDS)[reason];
  return `${words ?? 'The run did not apply.'} ${NOTHING}`;
}

// What a row's reason code says. A code this build does not know is shown as
// the code, so it is never hidden.
export const ROW_WORDS = {
  invalid_member: 'The row lacks a valid unit, name or email, or has a phone number that is not valid',
  inconsistent_person: 'The email is also on another row, with another name or phone',
  phone_in_multiple_rows: 'The phone is also on another row, with another email',
  inconsistent_membership: 'The unit has other details on another row',
  identity_conflict: 'The email and the phone are two different members\'',
  ambiguous_member: 'More than one member could be this person',
  email_change_requires_administrator: 'The email is not the member\'s; an administrator changes it on the member\'s page',
  member_in_multiple_groups: 'The member is on the list under two emails',
  administrator_managed: 'An administrator manages the member; the row was not applied',
  people_count_differs: 'The people on the row are not the number it states',
};
export const rowWords = (code) => ROW_WORDS[code] ?? code;

// A row as the page shows it: "Row 14".
export const rowLabel = (row) => `Row ${row}`;

// After a disabled member's name, for each booking still to come:
// "· booking K7Q4MD is still to come; it was not cancelled".
export const BOOKING_REST = 'is still to come; it was not cancelled';

// Who started a run: the administrator, or a dash for a run no administrator started.
export const byWords = (by) => by ?? '–';

// The sentence under the title: "Started 2026-10-12, 09:05 by Sam Field; A/L ..., members.xlsx".
export function startedWords(run) {
  const parser = [run.parser_name, run.file].filter(Boolean).join(', ');
  const by = run.by ? ` by ${run.by}` : '';
  return `Started ${run.started}${by}${parser ? `; ${parser}` : ''}`;
}

// What a failed start says, in plain words.
export const START_WORDS = {
  'file-too-large': 'The file is larger than 5 MB.',
  'wrong-file-type': 'The parser reads another kind of file.',
  'unknown-parser': 'The parser was not found.',
  'bad-request': 'The request was not understood.',
  busy: 'Another change is being made. Try again in a moment.',
};
export function startFailure(result) {
  if (result.kind === 'offline') return 'The server could not be reached. Nothing was changed.';
  if (result.kind === 'forbidden') return 'This page is out of date. Reload it and try again.';
  return START_WORDS[result.code] ?? 'The import could not be started.';
}

// The page title of a run, and its breadcrumb.
export const runTitle = (number) => `Import ${number}`;
