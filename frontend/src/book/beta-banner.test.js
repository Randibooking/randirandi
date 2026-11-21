// SPDX-License-Identifier: AGPL-3.0-only
// The beta bar's colours on the server-drawn pages (fallback.css) are the ones
// the app's bar draws (BetaBanner.jsx, scheme C's test roles). Fails if they drift.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const read = (path) => readFileSync(new URL(path, import.meta.url), 'utf8');

test('the server pages draw the beta bar in the colours of the app\'s bar', () => {
  const rule = read('../fallback.css').match(/\.fallback-beta \{([^}]*)\}/)[1];
  const bar = read('./BetaBanner.jsx');
  assert.equal(rule.match(/background: ([^;]+);/)[1], bar.match(/backgroundColor: '([^']+)'/)[1]);
  assert.equal(rule.match(/[^-]color: ([^;]+);/)[1], bar.match(/[^-]color: '([^']+)'/)[1]);
});
