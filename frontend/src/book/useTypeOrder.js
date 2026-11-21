// SPDX-License-Identifier: AGPL-3.0-only
import { typeOrder } from './cart.js';
import { useChoices } from './useChoices.js';

// The organization's order of booking types: from the page's own choices, or
// read once when the page started on My page.
export function useTypeOrder(config) {
  return typeOrder(useChoices(config));
}
