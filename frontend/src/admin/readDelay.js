// SPDX-License-Identifier: AGPL-3.0-only
// How long useRead waits before it asks: only a change of the search text
// (`q`) is waited out, so typing is not asked for letter by letter. The first
// ask of a list, a change of any other filter, a page and a return to the list
// go at once. `previous` and `next` are the queries as JSON text; `previous`
// is null before the first ask.
export const SEARCH_WAIT = 250;

const searchOf = (key) => JSON.parse(key)?.q;

export const readDelay = (previous, next) => (previous !== null && searchOf(previous) !== searchOf(next) ? SEARCH_WAIT : 0);
