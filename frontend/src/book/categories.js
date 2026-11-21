// SPDX-License-Identifier: AGPL-3.0-only
// The booking category's switch on Book: which icon a category has, and the
// ids the tabs and the calendar they change share.

// The keys a category's icon may be (CATEGORY_ICONS in randirandi/limits.py).
export const CATEGORY_ICON_KEYS = ['person', 'work', 'building', 'home', 'group'];

// The key when it is one of the list, else null: an unknown key shows no icon.
export const categoryIcon = (key) => (CATEGORY_ICON_KEYS.includes(key) ? key : null);

// The calendar region the tabs control, and each tab's own id (the region is
// named by the tab chosen).
export const CALENDAR_ID = 'booking-calendar';
export const categoryTabId = (id) => `booking-as-${id}`;
