// SPDX-License-Identifier: AGPL-3.0-only
import test from 'node:test';
import assert from 'node:assert/strict';
import {
  BOOKING_REST, COUNT_WORDS, OUTCOME_COLORS, ROW_WORDS, byWords, importsQuery, noteWords, rowLabel, rowWords, runTitle, startFailure,
  startedWords,
} from './imports.js';

test('the runs are asked in pages from 1', () => {
  assert.deepEqual(importsQuery(0), { page: 1 });
  assert.deepEqual(importsQuery(2), { page: 3 });
});

test('the outcomes have the drawing\'s colours and the counts its five words', () => {
  assert.deepEqual(OUTCOME_COLORS, { applied: 'success', stopped: 'warning', failed: 'error' });
  assert.deepEqual(COUNT_WORDS, ['New', 'Changed', 'Active again', 'Disabled', 'Skipped']);
});

test('an applied run has no note', () => {
  assert.equal(noteWords({ outcome: 'applied', reason: '' }), '');
});

test('a missing column names the heading the parser looked for', () => {
  assert.equal(
    noteWords({ outcome: 'failed', reason: 'missing_column', column: 'Email 1' }),
    'The parser found no column "Email 1" in the file; the file is not in the layout it reads. Nothing was changed.',
  );
  assert.equal(
    noteWords({ outcome: 'failed', reason: 'missing_column', column: '' }),
    'The parser found no column in the file; the file is not in the layout it reads. Nothing was changed.',
  );
});

test('the safety stop says how many of the active members, in whole percent, and the limit', () => {
  assert.equal(
    noteWords({ outcome: 'stopped', reason: 'revocation_limit', proposed: 41, active: 61 }),
    '41 of the 61 active members would have been disabled (67 %; the limit is 20 %). '
      + 'A list that short is most likely not the whole list. Nothing was changed.',
  );
  assert.match(noteWords({ outcome: 'stopped', reason: 'revocation_limit', proposed: 1, active: 0 }), /\(0 %/);
});

test('the other reasons have words, and one this build does not know still says nothing was changed', () => {
  assert.equal(noteWords({ outcome: 'failed', reason: 'invalid_xlsx' }), 'The file is not an Excel file the parser can read. Nothing was changed.');
  assert.equal(noteWords({ outcome: 'failed', reason: 'not_one_sheet' }).includes('more than one sheet'), true);
  assert.equal(noteWords({ outcome: 'failed', reason: 'external_reference' }).includes('another file'), true);
  assert.equal(noteWords({ outcome: 'stopped', reason: 'empty_list' }), 'The file has nobody on it. Nothing was changed.');
  assert.equal(noteWords({ outcome: 'stopped', reason: 'a_new_one' }), 'The run did not apply. Nothing was changed.');
  assert.equal(noteWords({ outcome: 'failed', reason: 'a_new_one' }), 'The run did not apply. Nothing was changed.');
});

test('a row\'s reason code is worded, and an unknown code is shown as it is', () => {
  for (const code of Object.keys(ROW_WORDS)) assert.notEqual(rowWords(code), code);
  assert.equal(rowWords('something_new'), 'something_new');
  assert.equal(rowLabel(14), 'Row 14');
  assert.equal(BOOKING_REST, 'is still to come; it was not cancelled');
});

test('who started a run, and the line under its title', () => {
  assert.equal(byWords('Sam Field'), 'Sam Field');
  assert.equal(byWords(null), '–');
  assert.equal(runTitle(5), 'Import 5');
  assert.equal(
    startedWords({ started: '2026-10-12, 09:05:00', by: 'Sam Field', parser_name: 'Unit members, Excel file', file: 'members.xlsx' }),
    'Started 2026-10-12, 09:05:00 by Sam Field; Unit members, Excel file, members.xlsx',
  );
  assert.equal(startedWords({ started: '2026-10-12, 09:05:00', by: null, parser_name: '', file: '' }), 'Started 2026-10-12, 09:05:00');
});

test('a failed start says what happened in plain words', () => {
  assert.equal(startFailure({ kind: 'offline' }), 'The server could not be reached. Nothing was changed.');
  assert.equal(startFailure({ kind: 'forbidden' }), 'This page is out of date. Reload it and try again.');
  assert.equal(startFailure({ kind: 'refused', code: 'file-too-large' }), 'The file is larger than 5 MB.');
  assert.equal(startFailure({ kind: 'refused', code: 'wrong-file-type' }), 'The parser reads another kind of file.');
  assert.equal(startFailure({ kind: 'refused', code: 'busy' }), 'Another change is being made. Try again in a moment.');
  assert.equal(startFailure({ kind: 'refused', code: null }), 'The import could not be started.');
});
