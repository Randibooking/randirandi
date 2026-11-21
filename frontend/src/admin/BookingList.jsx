// SPDX-License-Identifier: AGPL-3.0-only
import React from 'react';
import {
  Alert, Box, Link, MenuItem, Stack, Table, TableBody, TableCell, TableHead, TablePagination,
  TableRow, TableSortLabel, TextField, Typography,
} from '@mui/material';
import ChevronRightRounded from '@mui/icons-material/ChevronRightRounded';
import { readBookings } from './api';
import {
  BOOKING_ORDER, BOOKING_WHEN, addDays, bookingAddress, bookingsQuery, figure, todayIn,
} from './bookings';
import {
  Dates, Filters, MemberSearch, Progress, StateChip, StateFilter, boxStyle, cardStyle, openBooking, plainLink,
} from './parts';
import useRead from './useRead';

// The Bookings. Each is what a member made at one time (), as the server
// answers it in pages (/admin/bookings/list/); a row opens the booking.
export default function BookingList({ config, phone, open, again }) {
  const today = React.useMemo(() => todayIn(config.time_zone), [config.time_zone]);
  const [filters, setFilters] = React.useState({
    when: 'all', from: addDays(today, -6), to: today, state: 'all', q: '', order: 'number-desc', page: 0,
  });
  // The filters change the page; a new filter starts on the first.
  const change = (patch) => setFilters((f) => ({ ...f, page: 0, ...patch }));
  const { data, failed, loading } = useRead(readBookings, bookingsQuery(filters), again);
  const rows = data?.bookings ?? [];
  const [by, dir] = filters.order.split('-');
  const sortBy = (key) => change({ order: by === key ? `${key}-${dir === 'desc' ? 'asc' : 'desc'}` : `${key}-desc` });
  const head = (key, text) => <TableCell sortDirection={by === key ? dir : false}>
    <TableSortLabel active={by === key} direction={by === key ? dir : 'desc'} onClick={() => sortBy(key)}>{text}</TableSortLabel>
  </TableCell>;
  const go = (number) => openBooking(open, number);
  const states = <StateFilter value={filters.state} set={(state) => change({ state })} states={data?.states ?? []} phone={phone} />;
  const dates = <Stack spacing={1.5}>
    <Dates label="Created" choices={BOOKING_WHEN} when={filters.when} setWhen={(when) => change({ when })}
      from={filters.from} setFrom={(from) => change({ from })} to={filters.to} setTo={(to) => change({ to })} phone={phone} />
    {phone && <TextField select size="small" label="Order" value={filters.order} onChange={(e) => change({ order: e.target.value })}>
      {BOOKING_ORDER.map(([k, l]) => <MenuItem key={k} value={k}>{l}</MenuItem>)}
    </TextField>}
  </Stack>;
  return <Stack spacing={2}>
    <Filters phone={phone} dates={dates}>
      {states}<MemberSearch value={filters.q} set={(q) => change({ q })} phone={phone} placeholder="Member or reference" />
    </Filters>
    {failed && <Alert severity="error">The bookings could not be loaded.</Alert>}
    <Progress loading={loading} />
    {data && (phone ? <Stack spacing={1}>
      {rows.map((b) => <Box key={b.number} onClick={() => open(b.number)} sx={cardStyle}>
        <Stack direction="row" spacing={1} sx={{ alignItems: 'center' }}>
          <Box sx={{ flex: 1 }}>
            <Link href={bookingAddress(b.number)} onClick={go(b.number)} underline="none" color="inherit" variant="body2"
              sx={plainLink}>Reference {b.reference}</Link>
          </Box>
          <StateChip state={b.state} label={b.state_label} />
        </Stack>
        <Stack direction="row" sx={{ mt: 0.5 }}>
          <Typography variant="body2" sx={{ flex: 1 }}>{b.member}</Typography>
          <Typography variant="body2" sx={{ fontWeight: 600 }}>{figure(b.amount)} {b.currency}</Typography>
        </Stack>
        <Typography variant="caption" sx={{ color: 'text.secondary' }}>{b.categories} · created {b.created}</Typography>
      </Box>)}
      {!rows.length && <Typography sx={{ color: 'text.secondary', py: 2 }}>No booking matches.</Typography>}
    </Stack> : <Box sx={boxStyle}>
      <Table size="small" sx={{ '& td, & th': { whiteSpace: 'nowrap', px: 1.25 } }}>
        <TableHead><TableRow>
          {head('number', 'Reference')}{head('created', 'Created')}<TableCell>Category</TableCell>
          <TableCell>Member</TableCell><TableCell>State</TableCell><TableCell align="right">Amount</TableCell><TableCell>Currency</TableCell>
          <TableCell padding="checkbox" />
        </TableRow></TableHead>
        <TableBody>
          {rows.map((b) => <TableRow key={b.number} hover onClick={() => open(b.number)} sx={{ cursor: 'pointer' }}>
            <TableCell sx={{ fontWeight: 600 }}>
              <Link href={bookingAddress(b.number)} onClick={go(b.number)} underline="none" color="inherit" sx={plainLink}>{b.reference}</Link>
            </TableCell>
            <TableCell sx={{ color: 'text.secondary' }}>{b.created}</TableCell>
            <TableCell>{b.categories}</TableCell>
            <TableCell>{b.member}</TableCell>
            <TableCell><StateChip state={b.state} label={b.state_label} /></TableCell>
            <TableCell align="right">{figure(b.amount)}</TableCell>
            <TableCell sx={{ color: 'text.secondary' }}>{b.currency}</TableCell>
            <TableCell padding="checkbox"><ChevronRightRounded fontSize="small" sx={{ color: 'text.secondary' }} /></TableCell>
          </TableRow>)}
          {!rows.length && <TableRow><TableCell colSpan={8} sx={{ color: 'text.secondary', py: 4, textAlign: 'center' }}>No booking matches.</TableCell></TableRow>}
        </TableBody>
      </Table>
    </Box>)}
    {data && data.count > data.per_page && <TablePagination component="div" count={data.count} page={filters.page}
      rowsPerPage={data.per_page} rowsPerPageOptions={[data.per_page]}
      onPageChange={(_, page) => setFilters((f) => ({ ...f, page }))} />}
  </Stack>;
}
