// SPDX-License-Identifier: AGPL-3.0-only
import React from 'react';
import { Alert, Box, Button, CircularProgress, Snackbar, Stack } from '@mui/material';

// The bottom row of a sheet in the Booking types and categories and the Roles:
// the Remove on the left when there is one, then Cancel and Save.
export function SheetButtons({ close, save, ok = true, busy = false, left = null, label = 'Save' }) {
  return <Stack direction="row" spacing={1} sx={{ pt: 0.5 }}>
    {left}<Box sx={{ flex: 1 }} />
    <Button onClick={close} disabled={busy}>Cancel</Button>
    <Button type="submit" variant="contained" disabled={!ok || busy} onClick={save}
      startIcon={busy ? <CircularProgress size={16} color="inherit" /> : null}>{label}</Button>
  </Stack>;
}

export function Problems({ refused = [], problem = null }) {
  return <>
    {refused.map((text) => <Alert key={text} severity="error" variant="outlined">{text}</Alert>)}
    {problem && <Alert severity="error" variant="outlined">{problem}</Alert>}
  </>;
}

export function Toast({ said, clear }) {
  return <Snackbar open={!!said} autoHideDuration={2500} onClose={clear} message={said}
    anchorOrigin={{ vertical: 'bottom', horizontal: 'center' }} />;
}

// What a place keeps of the server's answers: the one it read, or the one a
// change answered with, until the place is read again.
export function useKept(data) {
  const [changed, setChanged] = React.useState(null);
  React.useEffect(() => { setChanged(null); }, [data]);
  return [changed ?? data, setChanged];
}
