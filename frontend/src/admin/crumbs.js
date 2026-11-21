// SPDX-License-Identifier: AGPL-3.0-only
// The breadcrumb of a place with pages under it (an asset, its floor plan, a
// container, a price page). The place tells the shell what is open (`labels`,
// outermost first) and how to go back to one of them; the shell draws
// "Admin > Place > Asset > Floor plan" on a wide screen. No DOM.
import React from 'react';
import { IMPORTS_ADDRESS, PAYOUTS_ADDRESS } from './bookings.js';

// The links and the last name of a breadcrumb: index 0 is the place itself, index
// n the n-th label. The last item is the page shown, not a link.
export function crumbItems(title, labels) {
  const all = [title, ...labels];
  return all.map((label, index) => ({ label, index, last: index === all.length - 1 }));
}

// The breadcrumb's items and what a click on each does. `action` is
// { type: 'crumb', index } (the place's own page at that index), { type: 'pick' }
// (the place's list) or { type: 'close' } (back to the page before). On the list
// the place and its open pages; on a booking, member or import run the place
// (and, for a booking, the pages the place has open) before it.
export function crumbTrail({ onList, title, labels, booking, run, payout }) {
  if (onList) {
    return crumbItems(title, labels).map((item) => ({ ...item, action: { type: 'crumb', index: item.index } }));
  }
  if (booking && labels.length) {
    return crumbItems(title, labels).map((item) => ({ ...item, last: false, action: item.index === 0 ? { type: 'pick' } : { type: 'close' } }));
  }
  const tab = run ? ['Imports', IMPORTS_ADDRESS] : payout ? ['Payouts', PAYOUTS_ADDRESS] : [title, undefined];
  return [{ label: tab[0], index: 0, last: false, action: { type: 'close' }, href: tab[1] }];
}

// A place registers the labels of what it has open with `set` (the shell's), and
// `go(index)`: show the page at that index (0 is the place's own list). Cleared
// when the place goes away. `go` may change every drawing; the labels are what
// the shell is told of.
export function useCrumb(set, labels, go) {
  const latest = React.useRef(go);
  latest.current = go;
  const key = labels.join('\u0001');
  React.useEffect(() => {
    if (!set) return;
    set(labels.length ? { labels, go: (index) => latest.current(index) } : null);
  }, [set, key]); // eslint-disable-line react-hooks/exhaustive-deps
  React.useEffect(() => () => { if (set) set(null); }, [set]);
}
