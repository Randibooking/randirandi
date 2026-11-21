// SPDX-License-Identifier: AGPL-3.0-only
import React from 'react';
import {
  Alert, Box, Button, LinearProgress, Link, Stack, Tab, Table, TableBody, TableCell, TableHead, TablePagination, TableRow, Tabs,
  Typography,
} from '@mui/material';
import ArrowBackRounded from '@mui/icons-material/ArrowBackRounded';
import { readPayout, readPayouts } from './api';
import { addDays, bookingAddress, figure, todayIn } from './bookings';
import { PAYOUTS_ADDRESS, PAYOUT_WHEN, amountOrNotYet, payoutAddress, payoutPageQuery, payoutTitle, payoutsQuery } from './payouts';
import { plainClick } from './route';
import { hrefOf } from './places';
import { Card, Dates, Field, OpenCard, OpenRow, Progress, boxStyle, openBooking, plainLink } from './parts';
import PaymentList from './PaymentList';
import useRead from './useRead';

// The Payments place's two tabs, and the Payouts and a payout's page. The list
// and the page are the server's answers for
// /admin/payments/payouts/list/ and /admin/payments/payouts/<id>/data/.

const tight = { '& td, & th': { whiteSpace: 'nowrap', px: 1.25 } };

// Each tab is a link to its own address; each stays drawn once shown, for its
// filters and page.
export function PaymentsPlace({ config, phone, again, open, tab, openTab, openPayout }) {
  const seen = React.useRef({});
  seen.current[tab] = true;
  const link = (value, label, href) => <Tab value={value} label={label} component="a" href={href}
    onClick={(event) => { if (plainClick(event)) { event.preventDefault(); openTab(value); } }} />;
  return <Stack spacing={2}>
    <Tabs value={tab} sx={{ borderBottom: 1, borderColor: 'divider', minHeight: 40, '& .MuiTab-root': { minHeight: 40 } }}>
      {link('payments', 'Payments', hrefOf('payments'))}
      {link('payouts', 'Payouts', PAYOUTS_ADDRESS)}
    </Tabs>
    {seen.current.payments && <Box sx={{ display: tab === 'payments' ? 'block' : 'none' }}>
      <PaymentList config={config} phone={phone} open={open} again={again} /></Box>}
    {seen.current.payouts && <Box sx={{ display: tab === 'payouts' ? 'block' : 'none' }}>
      <PayoutList config={config} phone={phone} again={again} openPayout={openPayout} /></Box>}
  </Stack>;
}

function PayoutList({ config, phone, again, openPayout }) {
  const today = React.useMemo(() => todayIn(config.time_zone), [config.time_zone]);
  const [filters, setFilters] = React.useState({ when: 'all', from: addDays(today, -6), to: today, page: 0 });
  const change = (patch) => setFilters((f) => ({ ...f, page: 0, ...patch }));
  const { data, failed, loading } = useRead(readPayouts, payoutsQuery(filters), again);
  const rows = data?.payouts ?? [];
  const go = (id) => (event) => {
    if (!plainClick(event)) return;
    event.preventDefault();
    openPayout(id);
  };
  return <Stack spacing={2}>
    <Dates label="Paid out" choices={PAYOUT_WHEN} when={filters.when} setWhen={(when) => change({ when })}
      from={filters.from} setFrom={(from) => change({ from })} to={filters.to} setTo={(to) => change({ to })} phone={phone} />
    {failed && <Alert severity="error">The payouts could not be loaded.</Alert>}
    <Progress loading={loading} />
    {data && (phone ? <Stack spacing={1}>
      {rows.map((x) => <OpenCard key={x.id} open={() => openPayout(x.id)}>
        <Stack direction="row">
          <Typography variant="body2" sx={{ fontWeight: 600, flex: 1 }}>{x.day}</Typography>
          <Typography variant="body2" sx={{ fontWeight: 600 }}>{figure(x.amount)} {x.currency}</Typography>
        </Stack>
        <Typography variant="caption" sx={{ color: 'text.secondary', overflowWrap: 'anywhere' }}>
          {x.payments} captured · {x.refunds} refunded · fees {figure(x.fees)} · {x.reference}</Typography>
      </OpenCard>)}
      {!rows.length && <Typography sx={{ color: 'text.secondary', py: 2 }}>None</Typography>}
    </Stack> : <Box sx={boxStyle}>
      <Table size="small" sx={tight}>
        <TableHead><TableRow>
          <TableCell>Paid out</TableCell><TableCell align="right">Payments</TableCell><TableCell align="right">Refunds</TableCell>
          <TableCell align="right">Captured</TableCell><TableCell align="right">Refunded</TableCell><TableCell align="right">Fees</TableCell>
          <TableCell align="right">Amount</TableCell><TableCell>Currency</TableCell><TableCell>Vipps reference</TableCell>
        </TableRow></TableHead>
        <TableBody>
          {rows.map((x) => <OpenRow key={x.id} open={() => openPayout(x.id)}>
            <TableCell><Link href={payoutAddress(x.id)} onClick={go(x.id)} underline="none" color="inherit" sx={plainLink}>{x.day}</Link></TableCell>
            <TableCell align="right">{x.payments}</TableCell><TableCell align="right">{x.refunds}</TableCell>
            <TableCell align="right">{figure(x.captured)}</TableCell><TableCell align="right">{figure(x.refunded)}</TableCell>
            <TableCell align="right">{figure(x.fees)}</TableCell>
            <TableCell align="right" sx={{ fontWeight: 600 }}>{figure(x.amount)}</TableCell>
            <TableCell sx={{ color: 'text.secondary' }}>{x.currency}</TableCell><TableCell>{x.reference}</TableCell>
          </OpenRow>)}
          {!rows.length && <TableRow><TableCell colSpan={9} sx={{ color: 'text.secondary', py: 4, textAlign: 'center' }}>None</TableCell></TableRow>}
        </TableBody>
      </Table>
    </Box>)}
    {data && data.count > data.per_page && <TablePagination component="div" count={data.count} page={filters.page}
      rowsPerPage={data.per_page} rowsPerPageOptions={[data.per_page]} onPageChange={(_, page) => setFilters((f) => ({ ...f, page }))} />}
  </Stack>;
}

// The question for the payout open, asked from the shell, which stays when the
// layout switches between a phone and a wide screen. Each question is a look-up
// the server writes down, so it is asked once for each payout opened and once
// for each page turned. The page on screen stays until the next one comes.
export function usePayout(id) {
  const [pages, setPages] = React.useState({ payments: 0, refunds: 0 });
  const [state, setState] = React.useState({ kind: 'loading' });
  React.useEffect(() => { setPages({ payments: 0, refunds: 0 }); }, [id]);
  React.useEffect(() => {
    if (id === null) return undefined;
    let current = true;
    readPayout(id, payoutPageQuery(pages)).then((answer) => {
      if (!current) return;
      if (answer.kind === 'signed-out') window.location.assign('/login/');
      else if (answer.ok) setState({ kind: 'found', payout: answer.data });
      else if (answer.kind === 'refused' && answer.status === 404) setState({ kind: 'missing' });
      else setState({ kind: 'failed' });
    });
    return () => { current = false; };
  }, [id, pages.payments, pages.refunds]); // eslint-disable-line react-hooks/exhaustive-deps
  return { ...state, pages, turn: (patch) => setPages((p) => ({ ...p, ...patch })) };
}

function Lines({ rows, phone, open, head, line, keyOf, empty }) {
  if (!rows.length) return <Typography variant="body2" sx={{ color: 'text.secondary', px: 2, pb: 1.5 }}>{empty}</Typography>;
  return phone ? rows.map((row) => <OpenCard flat key={keyOf(row)} open={() => open(row.number)}>{line.phone(row)}</OpenCard>)
    : <Table size="small" sx={tight}>
      <TableHead><TableRow>{head.map(([label, align]) => <TableCell key={label} align={align}>{label}</TableCell>)}</TableRow></TableHead>
      <TableBody>{rows.map((row) => <OpenRow key={keyOf(row)} open={() => open(row.number)}>{line.wide(row)}</OpenRow>)}</TableBody>
    </Table>;
}

export default function PayoutPage({ id, state: asked, phone, back, openBooking: openOrder }) {
  // An answer for another payout is not this one's.
  const state = asked.kind === 'found' && asked.payout.id !== id ? { kind: 'loading' } : asked;
  const backButton = phone && <Button startIcon={<ArrowBackRounded />} onClick={back} sx={{ alignSelf: 'flex-start' }}>Payouts</Button>;
  if (state.kind !== 'found') {
    return <Stack spacing={2}>
      {backButton}
      <Typography component="h1" variant="h5" sx={{ fontWeight: 600 }}>{payoutTitle()}</Typography>
      {state.kind === 'loading' && <LinearProgress />}
      {state.kind === 'missing' && <Typography sx={{ color: 'text.secondary' }}>None</Typography>}
      {state.kind === 'failed' && <Alert severity="error">The payout could not be loaded.</Alert>}
    </Stack>;
  }
  const x = state.payout;
  const { pages, turn } = asked;
  const booking = (row) => <Link href={bookingAddress(row.number)} onClick={openBooking(openOrder, row.number)} underline="none"
    color="inherit" sx={plainLink}>{row.order_reference}</Link>;
  const pager = (key, found) => found.pages > 1 && <TablePagination component="div" count={found.count} page={pages[key]}
    rowsPerPage={found.per_page} rowsPerPageOptions={[found.per_page]} onPageChange={(_, next) => turn({ [key]: next })} />;
  return <Stack spacing={2}>
    {backButton}
    <Typography component="h1" variant="h5" sx={{ fontWeight: 600 }}>{payoutTitle(x.day)}</Typography>
    <Card title="Payout">
      <Field label="Paid out">{x.day}</Field>
      <Field label="Amount">{figure(x.amount)}</Field>
      <Field label="Captured">{figure(x.captured)}</Field>
      <Field label="Refunded">{figure(x.refunded)}</Field>
      <Field label="Fees">{figure(x.fees)}</Field>
      {x.other !== 0 && <Field label="Other">{figure(x.other)}</Field>}
      <Field label="Currency">{x.currency}</Field>
      <Field label="Vipps reference">{x.reference}</Field>
      {x.statement && <Field label="Bank statement">{x.statement}</Field>}
    </Card>
    <Card flush title={`Payments (${x.payments})`}>
      <Lines rows={x.payment_rows} phone={phone} open={openOrder} keyOf={(row) => row.reference} empty="None" head={[
        ['Captured'], ['Booking'], ['Member'], ['Amount', 'right'], ['Fee', 'right'], ['Vipps reference']]}
      line={{
        wide: (row) => <>
          <TableCell sx={{ color: 'text.secondary' }}>{row.captured_at ?? '–'}</TableCell>
          <TableCell>{booking(row)}</TableCell><TableCell>{row.member}</TableCell>
          <TableCell align="right">{figure(row.captured)}</TableCell><TableCell align="right">{amountOrNotYet(row.fee)}</TableCell>
          <TableCell>{row.reference}</TableCell></>,
        phone: (row) => <>
          <Stack direction="row">
            <Typography variant="body2" sx={{ flex: 1 }}>{booking(row)}</Typography>
            <Typography variant="body2" sx={{ fontWeight: 600 }}>{figure(row.captured)} {x.currency}</Typography>
          </Stack>
          <Typography variant="caption" sx={{ color: 'text.secondary', overflowWrap: 'anywhere' }}>
            {row.member} · fee {amountOrNotYet(row.fee)}{row.captured_at ? ` · ${row.captured_at}` : ''}</Typography></>,
      }} />
      {pager('payments', x.payments_on_record)}
    </Card>
    {x.refunds > 0 && <Card flush title={`Refunds (${x.refunds})`}>
      <Lines rows={x.refund_rows} phone={phone} open={openOrder} keyOf={(row) => `${row.number}-${row.requested}-${row.amount}`} empty="None" head={[
        ['Requested'], ['Booking'], ['Member'], ['Amount', 'right'], ['By'], ['Vipps reference']]}
      line={{
        wide: (row) => <>
          <TableCell sx={{ color: 'text.secondary' }}>{row.requested}</TableCell>
          <TableCell>{booking(row)}</TableCell><TableCell>{row.member}</TableCell>
          <TableCell align="right">{figure(row.amount)}</TableCell><TableCell>{row.by ?? 'Automatic'}</TableCell>
          <TableCell>{row.reference || '–'}</TableCell></>,
        phone: (row) => <>
          <Stack direction="row">
            <Typography variant="body2" sx={{ flex: 1 }}>{booking(row)}</Typography>
            <Typography variant="body2" sx={{ fontWeight: 600 }}>{figure(row.amount)} {x.currency}</Typography>
          </Stack>
          <Typography variant="caption" sx={{ color: 'text.secondary' }}>{row.member} · {row.requested}</Typography></>,
      }} />
      {pager('refunds', x.refunds_on_record)}
    </Card>}
  </Stack>;
}
