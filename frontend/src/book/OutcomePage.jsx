// SPDX-License-Identifier: AGPL-3.0-only
import React from 'react';
import { Alert, Box, Button, CircularProgress, Divider, Link, Menu, MenuItem, Stack, Typography } from '@mui/material';
import CheckCircleRounded from '@mui/icons-material/CheckCircleRounded';
import HourglassTopRounded from '@mui/icons-material/HourglassTopRounded';
import EventBusyRounded from '@mui/icons-material/EventBusyRounded';
import EventRounded from '@mui/icons-material/EventRounded';
import { green } from '../theme/themePrimitives';
import { calendarChoices, groupLines, outcomeOf, paidWords, READ_EVERY, readOutcome } from './cart.js';
import { readOrder } from './api';
import { calendars } from './kept.js';
import { label } from './labels.js';
import LineGroups from './LineGroups';
import { useTypeOrder } from './useTypeOrder.js';

// What became of an order, ported from the operator's demo (`DonePage`; the
// not-paid case is `PayPage`'s release): confirmed with its lines grouped as in
// the cart, waiting for the payment (read again a few times), or not paid.
function Head({ icon, title, note }) {
  return <Stack spacing={1} sx={{ alignItems: 'center', textAlign: 'center', my: 3 }}>
    {icon}
    <Typography component="h1" variant="h4">{title}</Typography>
    {note && <Typography variant="body2" sx={{ color: 'text.secondary' }}>{note}</Typography>}
  </Stack>;
}

// "Add to calendar" (): the calendar file, and each booking in Google
// Calendar in a new tab; the address goes to Google only when chosen.
function AddToCalendar({ order }) {
  const [anchor, setAnchor] = React.useState(null);
  const choices = calendarChoices(order);
  if (!choices.length) return null;
  const close = () => setAnchor(null);
  return <>
    <Button id="add-to-calendar" variant="outlined" fullWidth startIcon={<EventRounded />} aria-haspopup="menu"
      aria-controls={anchor ? 'add-to-calendar-menu' : undefined} aria-expanded={anchor ? 'true' : undefined}
      onClick={(event) => setAnchor(event.currentTarget)} sx={{ mt: 2 }}>
      {label('outcome.add-to-calendar')}
    </Button>
    <Menu id="add-to-calendar-menu" anchorEl={anchor} open={Boolean(anchor)} onClose={close}
      slotProps={{ list: { 'aria-labelledby': 'add-to-calendar' }, paper: { sx: { maxWidth: 'calc(100vw - 32px)' } } }}>
      {choices.map((choice) => <MenuItem key={choice.key} component="a" href={choice.href} onClick={close} sx={{ whiteSpace: 'normal' }}
        {...(choice.file ? {} : { target: '_blank', rel: 'noopener noreferrer' })}>{choice.text}</MenuItem>)}
    </Menu>
  </>;
}

export default function OutcomePage({ config, orderId, link }) {
  const types = useTypeOrder(config);
  const [state, setState] = React.useState({ kind: 'loading' });
  const [reads, setReads] = React.useState(0);
  const [round, setRound] = React.useState(0);
  React.useEffect(() => {
    let live = true;
    let timer;
    readOrder(orderId).then((result) => {
      if (!live) return;
      const read = readOutcome(result);
      if (read.kind === 'signed-out') { window.location.assign('/login/'); return; }
      if (read.kind === 'order') {
        calendars.clear();
        setState(read);
        if (outcomeOf(read.order, reads).again) timer = setTimeout(() => setReads(reads + 1), READ_EVERY);
      } else {
        setState((was) => (was.kind === 'order' ? was : read));
      }
    });
    return () => { live = false; clearTimeout(timer); };
  }, [orderId, reads, round]);

  if (state.kind === 'loading') {
    return <Box sx={{ display: 'flex', justifyContent: 'center', py: 4 }}><CircularProgress aria-label={label('common.loading')} /></Box>;
  }
  if (state.kind === 'forbidden') {
    return <Stack spacing={1}><Typography>{label('common.no-access')}</Typography><Link href="/login/">{label('common.sign-in')}</Link></Stack>;
  }
  if (state.kind === 'not-found') {
    return <Stack spacing={1}><Typography>{label('outcome.not-found')}</Typography><Button {...link({ view: 'book' })}>{label('outcome.calendar')}</Button></Stack>;
  }
  if (state.kind !== 'order') {
    return <Alert severity="error" action={<Button color="inherit" size="small" onClick={() => { setState({ kind: 'loading' }); setRound(round + 1); }}>{label('common.try-again')}</Button>}>
      {label('common.could-not-load')}
    </Alert>;
  }
  const { order } = state;
  const shown = outcomeOf(order, reads);
  const groups = groupLines(order.bookings, types);
  const lines = <LineGroups groups={groups} currency={order.currency} />;
  if (shown.kind === 'confirmed') {
    return <Box>
      <Head icon={<CheckCircleRounded sx={{ fontSize: 56, color: green[400] }} />} title={label('outcome.booked')}
        note={label('outcome.order-paid', { reference: order.reference, paid: paidWords(order) })} />
      {lines}
      <AddToCalendar order={order} />
      <Stack direction="row" spacing={1.5} sx={{ mt: 2 }}>
        <Button variant="contained" {...link({ view: 'book' })} sx={{ flex: 1 }}>{label('outcome.book-more')}</Button>
        <Button variant="outlined" {...link({ view: 'me' })} sx={{ flex: 1 }}>{label('place.me')}</Button>
      </Stack>
    </Box>;
  }
  if (shown.kind === 'waiting') {
    return <Box>
      <Head icon={shown.again ? <CircularProgress aria-label={label('common.waiting')} /> : <HourglassTopRounded sx={{ fontSize: 56 }} />}
        title={label('outcome.waiting')} note={label('outcome.order', { reference: order.reference })} />
      {lines}
      <Stack direction="row" spacing={1.5} sx={{ mt: 2 }}>
        {order.state === 'held'
          ? <Button variant="contained" {...link({ view: 'cart' })} sx={{ flex: 1 }}>{label('outcome.go-to-payment')}</Button>
          : !shown.again && <Button variant="contained" onClick={() => { setReads(0); setRound(round + 1); }} sx={{ flex: 1 }}>{label('outcome.check-again')}</Button>}
        <Button variant="outlined" {...link({ view: 'me' })} sx={{ flex: 1 }}>{label('place.me')}</Button>
      </Stack>
    </Box>;
  }
  if (shown.kind === 'cancelled') {
    return <Box>
      <Head icon={<EventBusyRounded sx={{ fontSize: 56, color: 'text.secondary' }} />} title={label('outcome.cancelled')}
        note={label('outcome.order', { reference: order.reference })} />
      {lines}
      <Stack direction="row" spacing={1.5} sx={{ mt: 2 }}>
        <Button variant="contained" {...link({ view: 'book' })} sx={{ flex: 1 }}>{label('outcome.calendar')}</Button>
        <Button variant="outlined" {...link({ view: 'me' })} sx={{ flex: 1 }}>{label('place.me')}</Button>
      </Stack>
    </Box>;
  }
  return <Box>
    <Head icon={<EventBusyRounded sx={{ fontSize: 56, color: 'text.secondary' }} />} title={label('outcome.not-paid')}
      note={label('outcome.released')} />
    <Divider sx={{ mb: 2 }} />
    <Stack direction="row" spacing={1.5}>
      <Button variant="contained" {...link({ view: 'cart' })} sx={{ flex: 1 }}>{label('title.cart')}</Button>
      <Button variant="outlined" {...link({ view: 'book' })} sx={{ flex: 1 }}>{label('outcome.calendar')}</Button>
    </Stack>
  </Box>;
}
