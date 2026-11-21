// SPDX-License-Identifier: AGPL-3.0-only
// The Book page's Filter, from the member's choices answer
// (randirandi/core/booking_views.py `choices`). Pure: no DOM, no words but the
// names the server sent. A pick is the raw choice (the address's shape):
//   {category, type, asset, space, quantity, group}, each a number or null.
// `filterOf` turns a pick into what is chosen, with every choice the member can
// no longer have fallen back to its default.

const NUMBER = /^\d+$/;
const NAMES = ['category', 'type', 'asset', 'space', 'quantity', 'group'];

// The pages the Book place can show, in the order of the assets: an asset
// outside a space; a space shown whole; a space shown one asset at a time.
function pagesOf(choices) {
  const assets = choices?.assets || [];
  const spaces = new Map((choices?.spaces || []).map((space) => [space.id, space]));
  const pages = [];
  const seen = new Set();
  for (const asset of assets) {
    const space = spaces.get(asset.space);
    if (!space) {
      pages.push({ key: `asset:${asset.id}`, space: null, name: asset.name, description: asset.description || '', assets: [asset] });
    } else if (!seen.has(space.id)) {
      seen.add(space.id);
      pages.push({
        key: `space:${space.id}`,
        space,
        name: space.name,
        description: '',
        assets: assets.filter((row) => row.space === space.id),
      });
    }
  }
  return pages;
}

const wholeSpace = (page) => page.space?.booking_page === 'whole_space';

function pageOf(pages, pick) {
  const bySpace = pick.space == null ? null : pages.find((page) => page.space?.id === pick.space);
  const byAsset = pick.asset == null ? null : pages.find((page) => page.assets.some((asset) => asset.id === pick.asset));
  return bySpace || byAsset || pages[0];
}

// What the member is booking now: the pick, with every part that is gone or
// not offered at its default. Null when there is nothing to book.
export function filterOf(choices, pick = {}) {
  const categories = choices?.categories || [];
  const pages = pagesOf(choices);
  if (!pages.length || !categories.length) return null;
  const has = (list, id) => list.some((row) => row.id === id);
  const category = [pick.category, choices.category].find((id) => has(categories, id)) ?? categories[0].id;

  const page = pageOf(pages, pick);
  const whole = wholeSpace(page);
  const several = page.space && !whole;
  const asset = several ? (page.assets.find((row) => row.id === pick.asset) || page.assets[0]) : page.assets[0];
  const shown = whole ? page.assets : [asset];

  const types = [];
  for (const row of shown.flatMap((one) => one.types)) {
    if (row.categories.includes(category) && !has(types, row.id)) types.push({ id: row.id, name: row.name });
  }
  // A type of the page the category or the asset does not offer stays chosen
  // ("not switched away from"); the empty day says so and loosens it
  // . The number and the group then go by the category's own.
  const kept = pick.type == null || has(types, pick.type)
    ? null : page.assets.flatMap((one) => one.types).find((row) => row.id === pick.type);
  if (kept) types.push({ id: kept.id, name: kept.name });
  const type = kept ? kept.id : (types.length > 1 && has(types, pick.type) ? pick.type : null);
  const narrow = kept ? null : type;

  const offered = shown.flatMap((one) => one.types
    .filter((row) => (narrow == null || row.id === narrow) && row.categories.includes(category))
    .flatMap((row) => row.containers)
    .filter((container) => container.categories.includes(category))
    .map((container) => ({ container, asset: one })));
  const containers = offered.map((row) => row.container);
  const most = Math.max(1, ...containers
    .filter((container) => container.consumption_mode === 'shared' && container.max_quantity > 1)
    .map((container) => container.max_quantity));
  const quantity = pick.quantity >= 1 && pick.quantity <= most ? pick.quantity : 1;

  // The word for the spots the filtered containers sell: the floor plan's, when
  // they all share one; none otherwise (the page then uses the label).
  const words = new Set(containers
    .filter((container) => container.consumption_mode === 'shared' && container.max_quantity > 1)
    .map((container) => container.spot_word ?? null));
  const spotWord = words.size === 1 ? [...words][0] : null;

  // Keep us together: the groups of the filtered shared containers' floor
  // plans, each once, that hold the number asked for ; the calendar
  // answers nothing for a smaller one. A name two floor plans share says its
  // asset, or its floor plan's name, when it has one, on one asset.
  const groups = [];
  for (const { container, asset: one } of offered) {
    if (container.consumption_mode !== 'shared') continue;
    for (const row of container.groups || []) {
      if (row.spots >= quantity && !has(groups, row.id)) {
        groups.push({ id: row.id, name: row.name, word: row.word, spots: row.spots, asset: one.name, plan: row.plan });
      }
    }
  }
  const named = groups.map((row) => {
    const twins = groups.filter((other) => other.name === row.name);
    const by = new Set(twins.map((other) => other.asset)).size === twins.length ? row.asset : row.plan;
    return twins.length < 2 || !by ? row.name : `${row.name} · ${by}`;
  });
  groups.forEach((row, index) => {
    row.name = named[index];
    delete row.asset;
    delete row.plan;
  });
  const group = has(groups, pick.group) ? pick.group : null;
  // A group kept together holds no more than its spots.
  const ceiling = group == null ? most : Math.min(most, groups.find((row) => row.id === group).spots);

  return {
    page: { key: page.key, name: page.name, description: page.description },
    asset: asset.id,
    assets: several ? page.assets.map((row) => ({ id: row.id, name: row.name })) : [],
    categories,
    category,
    defaultCategory: has(categories, choices.category) ? choices.category : categories[0].id,
    types: types.length > 1 || kept ? types : [],
    type,
    quantities: ceiling > 1 ? Array.from({ length: ceiling }, (_, i) => i + 1) : [],
    quantity,
    spotWord,
    groups,
    group,
    // The page the address leaves out: the first.
    first: { page: pages[0].key === page.key, asset: page.assets[0].id === asset.id },
    pageSpace: whole ? page.space.id : null,
  };
}

// The choices as a pick, to change one and ask again.
export function pickOf(filter) {
  return {
    category: filter.category,
    type: filter.type,
    asset: filter.pageSpace == null ? filter.asset : null,
    space: filter.pageSpace,
    quantity: filter.quantity,
    group: filter.group,
  };
}

// The address's choices (`?category=2&type=5&asset=3&quantity=2&group=1`);
// anything that is not a number is no choice.
export function readAddress(search) {
  const query = new URLSearchParams(search);
  const pick = {};
  for (const name of NAMES) {
    const value = query.get(name);
    pick[name] = value !== null && NUMBER.test(value) ? Number(value) : null;
  }
  return pick;
}

// The query string (no "?") a filter is the link of. The category is always
// in it when the member has a choice, so a shared link keeps it; other
// defaults are left out.
export function addressOf(filter) {
  const query = new URLSearchParams();
  if (filter.categories.length > 1) query.set('category', filter.category);
  if (filter.type != null) query.set('type', filter.type);
  if (filter.pageSpace != null) {
    if (!filter.first.page) query.set('space', filter.pageSpace);
  } else if (!filter.first.page || !filter.first.asset) {
    query.set('asset', filter.asset);
  }
  if (filter.quantity !== 1) query.set('quantity', filter.quantity);
  if (filter.group != null) query.set('group', filter.group);
  return query.toString();
}

// What `readCalendar` takes besides `start` and `days`. No length: each
// flexible container is answered at its own minimum.
export function calendarQuery(filter) {
  const query = { category: filter.category };
  if (filter.pageSpace != null) query.space = filter.pageSpace;
  else query.asset = filter.asset;
  if (filter.type != null) query.type = filter.type;
  if (filter.quantity !== 1) query.quantity = filter.quantity;
  if (filter.group != null) query.group = filter.group;
  return query;
}
