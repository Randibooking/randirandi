// SPDX-License-Identifier: AGPL-3.0-only
import React from 'react';
import { readChoices } from './api';

// The member's choices (categories, assets, types): from the page's own, or read
// once when the page started on My page. Null until they are in.
export function useChoices(config) {
  const [choices, setChoices] = React.useState(config.choices ?? null);
  React.useEffect(() => {
    if (config.choices) return undefined;
    let live = true;
    readChoices().then((result) => { if (live && result.ok) setChoices(result.data); });
    return () => { live = false; };
  }, []);
  return choices;
}
