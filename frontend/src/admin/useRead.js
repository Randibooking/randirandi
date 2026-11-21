// SPDX-License-Identifier: AGPL-3.0-only
import React from 'react';
import { readDelay } from './readDelay';

// A list asked of the server whenever its query changes: the answer, whether
// the last ask failed, whether one is on its way. A later question makes an
// earlier answer stale. The first ask goes at once; only a change of the
// search text is waited out (readDelay.js). `again` is a number the caller
// raises to ask the same query once more (a list shown again): the answer on
// screen stays until the new one comes.
export default function useRead(read, query, again = 0) {
  const [data, setData] = React.useState(null);
  const [failed, setFailed] = React.useState(false);
  const [loading, setLoading] = React.useState(true);
  const key = JSON.stringify(query);
  const asked = React.useRef(null);
  React.useEffect(() => {
    let current = true;
    setLoading(true);
    const ask = async () => {
      const answer = await read(JSON.parse(key));
      if (!current) return;
      setLoading(false);
      if (answer.kind === 'signed-out') {
        window.location.assign('/login/');
      } else if (answer.ok) {
        setFailed(false);
        setData(answer.data);
      } else {
        setFailed(true);
      }
    };
    const delay = readDelay(asked.current, key);
    asked.current = key;
    const wait = delay ? setTimeout(ask, delay) : null;
    if (!delay) ask();
    return () => { current = false; if (wait) clearTimeout(wait); };
  }, [read, key, again]);
  return { data, failed, loading };
}
