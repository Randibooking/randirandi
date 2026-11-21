// SPDX-License-Identifier: AGPL-3.0-only
// The price list's data, from the calendar's public answers, is the same on the
// page (prices.js `priceBook`) and on the server's page without JavaScript
// (randirandi/core/price_table.py): the golden files hold answers and the data
// both must reach; tests/test_public_price_table.py is the server's side of it.
//   price-book.golden.json        a made-up organization: both kinds of tiers, a
//                                 night in EUR, a free container, hours of a
//                                 category's own, cents.
//   price-book-fjordside.golden.json  the Fjordside setup (setups/fjordside.toml)
//                                 as the server answers it (kr, its hours,
//                                 categories and examples); `expected` is
//                                 priceBook's output.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { priceBook } from './prices.js';

const golden = (name) => JSON.parse(readFileSync(new URL(`./${name}`, import.meta.url), 'utf8'));

test('priceBook reaches the data the server\'s price table is held to', () => {
  const one = golden('price-book.golden.json');
  const found = priceBook(structuredClone(one.answers), one.categories, one.order);
  assert.deepEqual(found, one.expected);
  const blocks = found.flatMap((type) => type.blocks);
  assert.ok(blocks.some((block) => block.cells.some((cell) => cell.offered === false)));
  assert.ok(blocks.some((block) => block.cells.some((cell) => cell.lines?.some((line) => line.includes(','))) && block.examples.length));
  assert.ok(found.some((type) => type.night));
});

test('priceBook reaches the data of the Fjordside setup the server\'s table is held to', () => {
  const fjordside = golden('price-book-fjordside.golden.json');
  const found = priceBook(structuredClone(fjordside.answers), fjordside.categories, fjordside.order);
  assert.deepEqual(found, fjordside.expected);
  // What the file is for: kr, a column's own hours, examples in every column, a night, a category not offered.
  const blocks = found.flatMap((type) => type.blocks);
  assert.ok(blocks.every((block) => block.cells.every((cell) => !cell.lines || cell.lines.every((line) => !line.includes('NOK')))));
  assert.ok(blocks.some((block) => block.cells.some((cell) => cell.lines?.some((line) => line.includes(' kr')))));
  assert.ok(blocks.some((block) => block.examples.length >= 2));
  assert.ok(found.some((type) => type.night));
  assert.ok(blocks.some((block) => block.cells.some((cell) => cell.offered === false)));
});
