// SPDX-License-Identifier: AGPL-3.0-only
import test from 'node:test';
import assert from 'node:assert/strict';
import {
  assetBody, assetFailure, blocksOf, clampCount, countOk, groupBody, groupTaken, heldWords, offeredWords, planBody, planName,
  planNeedsName, planTaken, spaceBody, spotBody, spotTaken, periodText, weekdaysText, wordOk, occupancyOk,
} from './assets.js';

const plan = {
  spots: [
    { id: 11, number: 2, name: 'Window', group: 5 }, { id: 10, number: 1, name: '', group: null },
    { id: 12, number: 3, name: '', group: 5 },
  ],
  groups: [{ id: 5, number: 1, name: 'Row A', word: 'row', spots: 2 }, { id: 6, number: 2, name: 'Row B', word: 'row', spots: 0 }],
};

test('a floor plan without a name is called by its place among the others', () => {
  const plans = [{ id: 1, name: '' }, { id: 2, name: 'Deck' }];
  assert.equal(planName(plans[0], plans), 'Floor plan 1');
  assert.equal(planName(plans[1], plans), 'Deck');
  assert.equal(planNeedsName(plans, 1), true);
  assert.equal(planNeedsName([plans[0]], 1), false);
  assert.equal(planTaken(plans, ' deck ', 1), true);
  assert.equal(planTaken(plans, 'deck', 2), false);
  assert.equal(planTaken(plans, '', 1), false);
});

test('a save sends only what differs', () => {
  const space = { name: 'Loft', description: '', booking_page: 'one_asset' };
  assert.deepEqual(spaceBody(space, { name: 'Loft ', description: '', booking_page: 'one_asset' }), {});
  assert.deepEqual(spaceBody(space, { name: 'Loft', description: ' Up ', booking_page: 'whole_space' }), { description: 'Up', booking_page: 'whole_space' });
  assert.deepEqual(spaceBody(null, { name: 'New', description: '', booking_page: 'one_asset' }), { name: 'New' });
  const asset = { name: 'Boat', description: '', space: null, whole_space: false, maximum_occupancy: null, time_zone: 'Europe/Oslo' };
  const draft = { name: 'Boat', description: '', space: '', whole_space: false, maximum_occupancy: '', time_zone: 'Europe/Oslo' };
  assert.deepEqual(assetBody(asset, draft, 'Europe/Oslo'), {});
  assert.deepEqual(assetBody(asset, { ...draft, space: '4', whole_space: true, maximum_occupancy: ' 8 ' }, 'Europe/Oslo'),
    { space: 4, whole_space: true, maximum_occupancy: 8 });
  // A mark without a space is not sent.
  assert.deepEqual(assetBody(asset, { ...draft, whole_space: true }, 'Europe/Oslo'), {});
  assert.deepEqual(assetBody(null, { ...draft, name: ' Raft ' }, 'Europe/Oslo'), { name: 'Raft' })
  assert.deepEqual(assetBody(null, { ...draft, name: 'Raft', time_zone: 'Asia/Tokyo' }, 'Europe/Oslo'), { name: 'Raft', time_zone: 'Asia/Tokyo' })
  assert.deepEqual(assetBody(asset, { ...draft, time_zone: '' }, 'Europe/Oslo'), {});
  assert.deepEqual(planBody({ name: 'A', word: 'seat' }, { name: 'A', word: ' bench ' }), { word: 'bench' });
  assert.deepEqual(planBody(null, { name: '', word: 'seat' }), { name: '', word: 'seat' });
  assert.deepEqual(spotBody(plan.spots[0], { name: 'Window', group: '5' }), {});
  assert.deepEqual(spotBody(plan.spots[0], { name: '', group: 'none' }), { name: '', group: null });
  assert.deepEqual(groupBody(plan.groups[0], { name: 'Row A', word: 'row', spots: [12, 11] }), { spots: [11, 12] });
  assert.deepEqual(groupBody(null, { name: 'New', word: '', spots: [] }), { name: 'New', spots: [] });
});

test('the checks that spare a round trip', () => {
  assert.equal(wordOk(' x '), true);
  assert.equal(wordOk(' '), false);
  assert.equal(wordOk('x'.repeat(81)), false);
  assert.equal(occupancyOk(''), true);
  assert.equal(occupancyOk('12'), true);
  assert.equal(occupancyOk('0'), false);
  assert.equal(occupancyOk('1.5'), false);
  assert.equal(countOk(500, 500), true);
  assert.equal(countOk(501, 500), false);
  assert.equal(clampCount('9000', 500), 500);
  assert.equal(clampCount('', 500), 1);
  assert.equal(spotTaken(plan, ' window ', 10).number, 2);
  assert.equal(spotTaken(plan, 'Window', 11), null);
  assert.equal(groupTaken(plan, 'row b', 5), true);
  assert.equal(groupTaken(plan, 'Row A', 5), false);
});

test('the spots come in blocks by group, those in none last', () => {
  const blocks = blocksOf(plan);
  assert.deepEqual(blocks.map((b) => [b.key, b.spots.map((s) => s.number)]), [[5, [2, 3]], [6, []], ['none', [1]]]);
  assert.deepEqual(blocksOf({ spots: [], groups: [] }), []);
});

test('what holds a row, and the words of a refusal', () => {
  assert.equal(heldWords({ containers: 1, bookings: 3 }), 'In use: 1 container, 3 bookings.');
  assert.equal(heldWords({ availability: 1 }), 'In use: availability.');
  assert.equal(heldWords({}), 'It is in use.');
  assert.equal(assetFailure({ kind: 'refused', code: 'in-use', body: { used: { assets: 2 } } }), 'In use: 2 assets.');
  assert.equal(assetFailure({ kind: 'refused', code: 'name-taken', body: { used: { spot: 4 } } }), 'Taken by spot 4');
  assert.equal(assetFailure({ kind: 'refused', code: 'name-taken', body: {} }), 'Taken');
  assert.equal(assetFailure({ kind: 'refused', code: 'time-zone', body: { message: 'Its zone stays.' } }), 'Its zone stays.');
  assert.equal(assetFailure({ kind: 'refused', code: 'last-spot', body: {} }), 'A floor plan keeps one spot.');
  assert.equal(assetFailure({ kind: 'offline' }), 'The server could not be reached. Nothing was changed.');
  assert.equal(assetFailure({ kind: 'refused', code: 'zzz', body: {} }), 'The change could not be made.');
  assert.equal(offeredWords({ offered: 'yes' }), 'Offered');
  assert.equal(offeredWords({ offered: 'no-container' }), 'No container');
});

test('a container reads as the asset page lists it', () => {
  assert.equal(periodText({ period: 'flexible', start: 600, end: 1320, interval: 60 }), '10:00–22:00, 60 min');
  assert.equal(periodText({ period: 'fixed', start: 1320, end: 1800, end_day: 1 }), '22:00 to 06:00 next day');
  assert.equal(weekdaysText({ weekdays: [0, 6] }), 'Mon Sun');
});
