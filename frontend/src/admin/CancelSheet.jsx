// SPDX-License-Identifier: AGPL-3.0-only
import React from 'react';
import { Alert, Box, Button, CircularProgress, FormControlLabel, Stack, Switch, Typography } from '@mui/material';
import { Sheet } from '../book/AddSheet';
import { moneyWords } from '../book/add.js';
import { cancelBooking } from './api';
import { cancelBody, cancelFailure, freeUntilWords, refundShown, targetsAmount } from './bookings';

// The question before a cancel: the times and the
// member, the refund switch (on, shown only for money paid and an amount above
// 0), and Keep / Cancel. The request key is new each time the sheet opens, so
// a second press of Cancel is the same request and cancels once.
export default function CancelSheet({ booking, line, targets, phone, csrf, onClose, onDone }) {
  const whole = line === null;
  const shown = refundShown(booking, targets);
  const [refund, setRefund] = React.useState(true);
  const [busy, setBusy] = React.useState(false);
  const [problem, setProblem] = React.useState(null);
  const key = React.useRef(null);
  if (key.current === null) key.current = crypto.randomUUID();
  const word = whole ? 'booking' : 'line';
  const send = async () => {
    if (busy) return;
    setBusy(true);
    setProblem(null);
    const result = await cancelBooking(csrf, booking.number, cancelBody({ key: key.current, line, refund, shown }));
    if (result.kind === 'signed-out') {
      window.location.assign('/login/');
      return;
    }
    setBusy(false);
    if (result.ok) onDone(result.data);
    else setProblem(cancelFailure(result));
  };
  const title = <Typography variant="h6" sx={{ fontWeight: 600 }}>
    {whole ? `Cancel booking ${booking.reference}?` : `Cancel one line of booking ${booking.reference}?`}</Typography>;
  return <Sheet wide={!phone} title={title} onClose={busy ? () => {} : onClose}>
    <Stack spacing={2}>
      <Box>
        {targets.map((l) => <Box key={l.id}>
          <Typography variant="body2">{l.start_date} {l.start_time} to {l.end_date} {l.end_time}</Typography>
          {freeUntilWords(l) && <Typography variant="body2" sx={{ color: 'text.secondary' }}>{freeUntilWords(l)}</Typography>}
        </Box>)}
        <Typography variant="body2" sx={{ color: 'text.secondary' }}>{booking.member.name}</Typography>
      </Box>
      {shown && <FormControlLabel
        control={<Switch checked={refund} onChange={(e) => setRefund(e.target.checked)} disabled={busy} />}
        label={`Refund ${moneyWords(targetsAmount(targets), booking.currency)} through Vipps`} />}
      {problem && <Alert severity="error">{problem}</Alert>}
      <Stack direction="row" spacing={1} sx={{ justifyContent: 'flex-end' }}>
        <Button onClick={onClose} disabled={busy}>Keep {word}</Button>
        <Button variant="contained" color="error" onClick={send} disabled={busy}
          startIcon={busy ? <CircularProgress size={16} color="inherit" /> : null}>Cancel {word}</Button>
      </Stack>
    </Stack>
  </Sheet>;
}
