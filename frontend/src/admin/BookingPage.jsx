// SPDX-License-Identifier: AGPL-3.0-only
import React from 'react';
import {
  Alert, Box, Button, Collapse, Divider, LinearProgress, Stack, Table, TableBody, TableCell, TableHead, TableRow,
  IconButton, Link, Typography,
} from '@mui/material';
import ArrowBackRounded from '@mui/icons-material/ArrowBackRounded';
import ExpandMoreRounded from '@mui/icons-material/ExpandMoreRounded';
import { moneyWords } from '../book/add.js';
import { red } from '../theme/themePrimitives';
import { readBooking } from './api';
import { TIME_HEADS, cancelPlan, cancelTargets, figure, memberAddress, priceWords } from './bookings';
import CancelSheet from './CancelSheet';
import { amountOrNotYet, paidOutOn } from './payouts';
import { Card, Field, PaymentChip, StateChip } from './parts';
import { plainClick } from './route';

// One booking. Every word and number is the server's answer for
// /admin/bookings/<number>/data/, built from the records; reading it writes
// one "inspected" event. Cancelling the booking or a line (part 7.7) asks
// first (CancelSheet); the server's answer to the cancel is the page again.
const struck = (line) => (line.state === 'cancelled' ? { textDecoration: 'line-through', color: 'text.secondary' } : {});

function Times({ line }) {
  return [line.start_date, line.start_time, line.end_date, line.end_time].map((text, i) =>
    <TableCell key={TIME_HEADS[i]} sx={{ whiteSpace: 'nowrap', ...struck(line) }}>{text}</TableCell>);
}

// The dark theme's error.light is the darker red (3.07:1 on the page), so the
// cancel buttons take red[300] (5.84:1 on the page's rgb(11, 14, 20)); "Cancel
// this line" sits on the opened line's rgb(51, 60, 77) and takes red[200] (5.48:1).
function Prices({ line, currency, onCancel }) {
  return <Box sx={{ px: 2, py: 1.5 }}>
    {line.prices.length ? <Box sx={{ border: 1, borderColor: 'divider', borderRadius: 2, p: 1.5 }}>
      {line.prices.map((price, i) => <Stack key={i} direction="row" sx={{ justifyContent: 'space-between', gap: 2, py: 0.25 }}>
        <Typography variant="body2" sx={{ color: 'text.secondary' }}>{priceWords(price)}</Typography>
        <Typography variant="body2">{moneyWords(price.total, currency)}</Typography>
      </Stack>)}
    </Box> : <Typography variant="body2" sx={{ color: 'text.secondary' }}>Free of charge</Typography>}
    {onCancel && <Button size="small" sx={{ mt: 1, color: red[200] }} onClick={() => onCancel(line)}>Cancel this line</Button>}
  </Box>;
}

function LineRow({ line, currency, phone, onCancel }) {
  const [shown, setShown] = React.useState(false);
  const toggle = <ExpandMoreRounded fontSize="small"
    sx={{ color: 'text.secondary', transform: shown ? 'rotate(180deg)' : 'none', transition: 'transform .15s' }} />;
  if (phone) {
    return <Box>
      <Stack direction="row" spacing={1.5} onClick={() => setShown(!shown)} role="button" tabIndex={0} aria-expanded={shown}
        onKeyDown={(e) => (e.key === 'Enter' || e.key === ' ') && (e.preventDefault(), setShown(!shown))}
        sx={{ alignItems: 'center', px: 2, py: 1.1, cursor: 'pointer', '&:hover': { bgcolor: 'action.hover' } }}>
        <Box sx={{ flex: 1, minWidth: 0 }}>
          <Typography variant="body2" sx={{ fontWeight: 600, ...struck(line) }}>{line.start_date} {line.start_time}</Typography>
          <Typography variant="body2" sx={{ color: 'text.secondary', ...struck(line) }}>to {line.end_date} {line.end_time}</Typography>
          <Typography variant="caption" sx={{ color: 'text.secondary' }}>
            {line.booking_type} · {line.category} · {figure(line.amount)} {currency}</Typography>
        </Box>
        <StateChip state={line.state} label={line.state_label} />
        {toggle}
      </Stack>
      <Collapse in={shown}><Prices line={line} currency={currency} onCancel={onCancel} /></Collapse>
    </Box>;
  }
  return <>
    <TableRow hover onClick={() => setShown(!shown)} sx={{ cursor: 'pointer' }}>
      <Times line={line} />
      <TableCell>{line.booking_type}</TableCell>
      <TableCell>{line.category}</TableCell>
      <TableCell><StateChip state={line.state} label={line.state_label} /></TableCell>
      <TableCell align="right">{figure(line.amount)}</TableCell>
      <TableCell padding="checkbox">
        <IconButton size="small" aria-expanded={shown} aria-label={`Show line ${line.start_date} ${line.start_time}`}
          onClick={(e) => { e.stopPropagation(); setShown(!shown); }}>{toggle}</IconButton>
      </TableCell>
    </TableRow>
    <TableRow><TableCell colSpan={9} sx={{ p: 0, ...(shown ? {} : { borderBottom: 'none' }) }}>
      <Collapse in={shown}><Prices line={line} currency={currency} onCancel={onCancel} /></Collapse>
    </TableCell></TableRow>
  </>;
}

// A booking's refunds (DATA-MODEL, Refund): read only.
function Refunds({ refunds, currency, phone }) {
  const lineText = (line) => (line ? `${line.start_date} ${line.start_time}` : '–');
  return <Card flush title={`Refunds (${refunds.length})`}>
    {phone ? refunds.map((r) => <React.Fragment key={r.id}>
      <Divider />
      <Box sx={{ px: 2, py: 1.1 }}>
        <Stack direction="row">
          <Typography variant="body2" sx={{ fontWeight: 600, flex: 1 }}>{r.requested}</Typography>
          <Typography variant="body2" sx={{ fontWeight: 600 }}>{figure(r.amount)} {currency}</Typography>
        </Stack>
        <Typography variant="caption" sx={{ color: 'text.secondary' }}>
          {r.automatic ? 'Automatic' : r.by} · {lineText(r.line)}{r.reference ? ` · ${r.reference}` : ''}</Typography>
      </Box>
    </React.Fragment>) : <Table size="small" sx={{ '& td, & th': { whiteSpace: 'nowrap' } }}>
      <TableHead><TableRow>
        <TableCell>Requested</TableCell><TableCell>Completed</TableCell><TableCell align="right">Amount ({currency})</TableCell>
        <TableCell>Booking line</TableCell><TableCell>By</TableCell><TableCell>Vipps reference</TableCell>
      </TableRow></TableHead>
      <TableBody>
        {refunds.map((r) => <TableRow key={r.id}>
          <TableCell>{r.requested}</TableCell><TableCell>{r.completed ?? '–'}</TableCell>
          <TableCell align="right">{figure(r.amount)}</TableCell><TableCell>{lineText(r.line)}</TableCell>
          <TableCell>{r.automatic ? 'Automatic' : r.by}</TableCell><TableCell>{r.reference || '–'}</TableCell>
        </TableRow>)}
      </TableBody>
    </Table>}
  </Card>;
}

function History({ events }) {
  return <Stack spacing={1}>
    {events.map((event, i) => <Stack key={i} direction="row" spacing={2}>
      <Typography variant="body2" sx={{ color: 'text.secondary', width: 170, flexShrink: 0, whiteSpace: 'nowrap' }}>{event.at}</Typography>
      <Typography variant="body2">{event.text}</Typography>
    </Stack>)}
  </Stack>;
}

// The question for the booking open, asked from the shell, which stays when
// the layout switches between a phone and a wide screen: each question is a
// look-up the server writes down, so it is asked once for each booking
// opened, never again for a redraw.
export function useBooking(number) {
  const [state, setState] = React.useState({ kind: 'loading' });
  React.useEffect(() => {
    if (number === null) return undefined;
    let current = true;
    setState({ kind: 'loading' });
    readBooking(number).then((answer) => {
      if (!current) return;
      if (answer.kind === 'signed-out') window.location.assign('/login/');
      else if (answer.ok) setState({ kind: 'found', booking: answer.data });
      else if (answer.kind === 'refused' && answer.status === 404) setState({ kind: 'missing' });
      else setState({ kind: 'failed' });
    });
    return () => { current = false; };
  }, [number]);
  // After a cancel the server answers with the booking again: shown as is,
  // without a second look-up (which would be written down).
  const replace = React.useCallback((booking) => setState({ kind: 'found', booking }), []);
  return { ...state, replace };
}

export default function BookingPage({ number, state: asked, phone, back, backLabel, csrf, openMember }) {
  const [asking, setAsking] = React.useState(null); // { line } of the sheet open
  // An answer for another booking is not this one's.
  const state = asked.kind === 'found' && asked.booking.number !== number ? { kind: 'loading' } : asked;
  const { booking: b } = state;
  const backButton = phone && <Button startIcon={<ArrowBackRounded />} onClick={back} sx={{ alignSelf: 'flex-start' }}>{backLabel}</Button>;
  if (state.kind !== 'found') {
    return <Stack spacing={2}>
      {backButton}
      <Typography component="h1" variant="h5" sx={{ fontWeight: 600 }}>Booking</Typography>
      {state.kind === 'loading' && <LinearProgress />}
      {state.kind === 'missing' && <Typography sx={{ color: 'text.secondary' }}>None</Typography>}
      {state.kind === 'failed' && <Alert severity="error">The booking could not be loaded.</Alert>}
    </Stack>;
  }
  const { payment, currency } = b;
  const plan = cancelPlan(b);
  const ask = (line) => setAsking({ line });
  return <Stack spacing={2}>
    {backButton}
    <Stack direction={phone ? 'column' : 'row'} spacing={1.5} sx={{ alignItems: phone ? 'flex-start' : 'center' }}>
      <Box sx={{ flex: 1 }}>
        <Stack direction="row" spacing={1} sx={{ alignItems: 'center' }}>
          <Typography component="h1" variant="h5" sx={{ fontWeight: 600 }}>Reference {b.reference}</Typography>
          <StateChip state={b.state} label={b.state_label} />
        </Stack>
        <Typography sx={{ color: 'text.secondary', mt: 0.5 }}>Created {b.created} by {b.member.name}</Typography>
      </Box>
      {plan.whole && <Button variant="outlined" sx={{ color: red[300], borderColor: red[400] }} onClick={() => ask(null)}>
        Cancel booking</Button>}
    </Stack>
    <Box sx={{ display: 'grid', gap: 2, gridTemplateColumns: phone ? '1fr' : '1fr 1fr' }}>
      <Card title="Member">
        <Field label="Name">
          <Link href={memberAddress(b.member.id)} variant="body2" underline="hover" color="inherit"
            onClick={(event) => { if (plainClick(event)) { event.preventDefault(); openMember(b.member.id); } }}>{b.member.name}</Link>
        </Field>
        <Field label="Email">{b.member.email || '–'}</Field>
        <Field label="Booked as">{b.member.booked_as}</Field>
      </Card>
      <Card title="Payment">
        {payment ? <>
          <Field label="State">
            <Stack direction="row" spacing={0.75}>
              <span>{payment.state_label}</span>
              {payment.attention && <PaymentChip state="attention" label="Needs attention" />}
            </Stack>
          </Field>
          {payment.trouble && <>
            <Field label="Failing since">{payment.trouble.since}</Field>
            <Field label="Step">{payment.trouble.step_label}</Field>
            <Field label="Why">{payment.trouble.kind_label}</Field>
            <Field label="Tries">{payment.trouble.tries}</Field>
          </>}
          {payment.missing_refund && <Field label="Refund">Not in Vipps's report</Field>}
          <Field label="Amount">{figure(payment.amount)}</Field>
          <Field label="Captured">{figure(payment.captured)}</Field>
          {payment.captured_at && <Field label="Captured at">{payment.captured_at}</Field>}
          {payment.captured > 0 && <>
            <Field label="Vipps fee">{amountOrNotYet(payment.fee)}</Field>
            <Field label="Net">{amountOrNotYet(payment.net)}</Field>
            <Field label="Paid out">{paidOutOn(payment.payout)}</Field>
          </>}
          <Field label="Refunded">{figure(payment.refunded)}</Field>
          <Field label="Currency">{currency}</Field>
          <Field label="Vipps reference">{payment.reference}</Field>
        </> : <Field label="State">No payment</Field>}
      </Card>
    </Box>
    <Card flush title={`Booking lines (${b.lines.length})`}>
      {phone ? b.lines.map((line) => <React.Fragment key={line.id}>
        <Divider />
        <LineRow line={line} currency={currency} phone onCancel={plan.line && line.coming ? ask : null} />
      </React.Fragment>) : <Table size="small">
        <TableHead><TableRow>
          {TIME_HEADS.map((h) => <TableCell key={h}>{h}</TableCell>)}
          <TableCell>Booking type</TableCell><TableCell>Category</TableCell><TableCell>State</TableCell>
          <TableCell align="right">Amount ({currency})</TableCell><TableCell padding="checkbox" />
        </TableRow></TableHead>
        <TableBody>{b.lines.map((line) => <LineRow key={line.id} line={line} currency={currency}
          onCancel={plan.line && line.coming ? ask : null} />)}</TableBody>
      </Table>}
    </Card>
    {b.refunds.length > 0 && <Refunds refunds={b.refunds} currency={currency} phone={phone} />}
    <Card title="History"><History events={b.history} /></Card>
    {asking && <CancelSheet booking={b} line={asking.line} targets={cancelTargets(b, asking.line)} phone={phone} csrf={csrf}
      onClose={() => setAsking(null)} onDone={(booking) => { setAsking(null); asked.replace(booking); }} />}
  </Stack>;
}
