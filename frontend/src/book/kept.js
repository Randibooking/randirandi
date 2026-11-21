// SPDX-License-Identifier: AGPL-3.0-only
// The month calendars kept in memory, shared by Book and the cart pages: a cart
// change anywhere clears them all (`store.clear()`).
import { createStore } from './store.js';

export const calendars = createStore();
