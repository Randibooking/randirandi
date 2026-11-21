// SPDX-License-Identifier: AGPL-3.0-only
import React from 'react';
import { assetFailure } from './assets';

// What both pages of the Assets and Spaces do around a change: one call at a
// time, the server's refusal in plain words, a signed-out browser sent to the
// login.
export function useRun(onDone) {
  const [busy, setBusy] = React.useState(false);
  const [problem, setProblem] = React.useState(null);
  const run = async (call, toast) => {
    if (busy) return;
    setBusy(true);
    setProblem(null);
    const result = await call();
    if (result.kind === 'signed-out') {
      window.location.assign('/login/');
      return;
    }
    setBusy(false);
    if (result.ok) onDone(result.data, toast);
    else setProblem(assetFailure(result));
  };
  return { busy, problem, setProblem, run };
}
