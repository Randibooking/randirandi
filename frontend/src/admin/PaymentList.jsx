// SPDX-License-Identifier: AGPL-3.0-only
import React from 'react';
import {
  Alert, Box, Link, Stack, Table, TableBody, TableCell, TableHead, TablePagination, TableRow,
  TableSortLabel, Typography,
} from '@mui/material';
import { readPayments } from './api';
import { BOOKING_WHEN, addDays, bookingAddress, figure, paymentsQuery, todayIn } from './bookings';
import {
  Dates, MemberSearch, PaymentChip, Progress, StateFilter, boxStyle, cardStyle, openBooking, plainLink,
} from './parts';
import useRead from './useRead';

// The Payments. One row per booking that has a payment, as the server answers
// it in pages (/admin/payments/list/); a row opens the booking.
export default function PaymentList({ config, phone, open, again }) {
  const today = React.useMemo(() => todayIn(config.time_zone), [config.time_zone]);
  const [filters, setFilters] = React.useState({
    when: 'all', from: addDays(today, -6), to: today, state: 'all', q: '', dir: 'desc', page: 0,
  });
  // The filters change the page; a new filter starts on the first.
  const change = (patch) => setFilters((f) => ({ ...f, page: 0, ...patch }));
  const { data, failed, loading } = useRead(readPayments, paymentsQuery(filters), again);
  const rows = data?.payments ?? [];
  const go = (number) => openBooking(open, number);
  const dates = <Dates label="Created" choices={BOOKING_WHEN} when={filters.when} setWhen={(when) => change({ when })}
    from={filters.from} setFrom={(from) => change({ from })} to={filters.to} setTo={(to) => change({ to })} phone={phone} />;
  const state = <StateFilter value={filters.state} set={(state) => change({ state })} states={data?.states ?? []} phone={phone} />;
  const search = <MemberSearch value={filters.q} set={(q) => change({ q })} phone={phone} placeholder="Member or reference" />;
  return <Stack spacing={2}>
    {phone ? <Stack spacing={1.5} sx={{ alignItems: 'flex-start' }}>{dates}{state}<Box sx={{ display: 'flex', width: '100%' }}>{search}</Box></Stack>
      : <Stack direction="row" sx={{ flexWrap: 'wrap', gap: 1.5, alignItems: 'center' }}>{dates}{state}{search}</Stack>}
    {failed && <Alert severity="error">The payments could not be loaded.</Alert>}
    <Progress loading={loading} />
    {data && (phone ? <Stack spacing={1}>
      {rows.map((p) => <Box key={p.number} onClick={() => open(p.number)} sx={cardStyle}>
        <Stack direction="row" spacing={1} sx={{ alignItems: 'center' }}>
          <Box sx={{ flex: 1 }}>
            <Link href={bookingAddress(p.number)} onClick={go(p.number)} underline="none" color="inherit" variant="body2"
              sx={plainLink}>Reference {p.order_reference}</Link>
          </Box>
          <PaymentChip state={p.attention ? 'attention' : p.state} label={p.attention ? 'Needs attention' : p.state_label} />
        </Stack>
        <Stack direction="row" sx={{ mt: 0.5 }}>
          <Typography variant="body2" sx={{ flex: 1 }}>{p.member}</Typography>
          <Typography variant="body2" sx={{ fontWeight: 600 }}>{figure(p.amount)} {p.currency}</Typography>
        </Stack>
        <Typography variant="caption" sx={{ color: 'text.secondary', overflowWrap: 'anywhere' }}>
          {p.reference} · {p.created}{p.refunded ? ` · refunded ${figure(p.refunded)} ${p.currency}` : ''}</Typography>
      </Box>)}
      {!rows.length && <Typography sx={{ color: 'text.secondary', py: 2 }}>No payment matches.</Typography>}
    </Stack> : <Box sx={boxStyle}>
      <Table size="small" sx={{ '& td, & th': { whiteSpace: 'nowrap', px: 0.75 } }}>
        <TableHead><TableRow>
          <TableCell sortDirection={filters.dir}>
            <TableSortLabel active direction={filters.dir}
              onClick={() => change({ dir: filters.dir === 'asc' ? 'desc' : 'asc' })}>Created</TableSortLabel>
          </TableCell>
          <TableCell>Reference</TableCell><TableCell>Member</TableCell><TableCell>State</TableCell>
          <TableCell align="right">Amount</TableCell><TableCell align="right">Captured</TableCell>
          <TableCell align="right">Refunded</TableCell><TableCell>Currency</TableCell><TableCell>Vipps reference</TableCell>
        </TableRow></TableHead>
        <TableBody>
          {rows.map((p) => <TableRow key={p.number} hover onClick={() => open(p.number)} sx={{ cursor: 'pointer' }}>
            <TableCell sx={{ color: 'text.secondary' }}>{p.created}</TableCell>
            <TableCell sx={{ fontWeight: 600 }}>
              <Link href={bookingAddress(p.number)} onClick={go(p.number)} underline="none" color="inherit" sx={plainLink}>{p.order_reference}</Link>
            </TableCell>
            <TableCell>{p.member}</TableCell>
            <TableCell>
              <PaymentChip state={p.attention ? 'attention' : p.state} label={p.attention ? 'Needs attention' : p.state_label} />
            </TableCell>
            <TableCell align="right">{figure(p.amount)}</TableCell>
            <TableCell align="right">{figure(p.captured)}</TableCell>
            <TableCell align="right">{figure(p.refunded)}</TableCell>
            <TableCell sx={{ color: 'text.secondary' }}>{p.currency}</TableCell>
            <TableCell title={p.reference} sx={{ maxWidth: '14rem', overflow: 'hidden', textOverflow: 'ellipsis' }}>{p.reference}</TableCell>
          </TableRow>)}
          {!rows.length && <TableRow><TableCell colSpan={9} sx={{ color: 'text.secondary', py: 4, textAlign: 'center' }}>No payment matches.</TableCell></TableRow>}
        </TableBody>
      </Table>
    </Box>)}
    {data && data.count > data.per_page && <TablePagination component="div" count={data.count} page={filters.page}
      rowsPerPage={data.per_page} rowsPerPageOptions={[data.per_page]}
      onPageChange={(_, page) => setFilters((f) => ({ ...f, page }))} />}
  </Stack>;
}
