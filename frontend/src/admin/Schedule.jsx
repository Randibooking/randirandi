// SPDX-License-Identifier: AGPL-3.0-only
import React from 'react';
import {
  Alert, Box, Link, Stack, Table, TableBody, TableCell, TableHead, TablePagination, TableRow,
  TableSortLabel, Typography,
} from '@mui/material';
import ChevronRightRounded from '@mui/icons-material/ChevronRightRounded';
import { readSchedule } from './api';
import {
  SCHEDULE_WHEN, TIME_HEADS, addDays, bookingAddress, scheduleQuery, startOrder, todayIn,
} from './bookings';
import { Dates, Filters, MemberSearch, Progress, StateChip, boxStyle, cardStyle, plainLink } from './parts';
import useRead from './useRead';

// The Schedule: the booked times that hold time, by their start (), on each
// asset's own clock; a row opens its booking.
export default function Schedule({ config, phone, open, again }) {
  const today = React.useMemo(() => todayIn(config.time_zone), [config.time_zone]);
  const [filters, setFilters] = React.useState({
    when: 'week', from: today, to: addDays(today, 6), q: '', dir: 'asc', page: 0,
  });
  const change = (patch) => setFilters((f) => ({ ...f, page: 0, ...patch }));
  const { data, failed, loading } = useRead(readSchedule, scheduleQuery(filters), again);
  const lines = data?.lines ?? [];
  const go = (number) => (event) => {
    event.stopPropagation();
    if (event.metaKey || event.ctrlKey || event.shiftKey || event.button === 1) return;
    event.preventDefault();
    open(number);
  };
  const dates = <Dates label="Booked for" choices={SCHEDULE_WHEN} when={filters.when}
    setWhen={(when) => change({ when, dir: startOrder(when) })} from={filters.from} setFrom={(from) => change({ from })}
    to={filters.to} setTo={(to) => change({ to })} phone={phone} />;
  return <Stack spacing={2}>
    <Filters phone={phone} dates={dates}><MemberSearch value={filters.q} set={(q) => change({ q })} phone={phone} placeholder="Member or reference" /></Filters>
    {failed && <Alert severity="error">The schedule could not be loaded.</Alert>}
    <Progress loading={loading} />
    {data && (phone ? <Stack spacing={1}>
      {lines.map((l) => <Box key={l.id} onClick={() => open(l.booking)} sx={cardStyle}>
        <Stack direction="row" spacing={1} sx={{ alignItems: 'flex-start' }}>
          <Box sx={{ flex: 1 }}>
            <Typography variant="body2" sx={{ fontWeight: 600 }}>{l.start_date} {l.start_time}</Typography>
            <Typography variant="body2" sx={{ color: 'text.secondary' }}>to {l.end_date} {l.end_time}</Typography>
          </Box>
          <Link href={bookingAddress(l.booking)} onClick={go(l.booking)} underline="none" color="inherit" variant="body2"
            sx={plainLink}>Reference {l.reference}</Link>
        </Stack>
        <Stack direction="row" spacing={1} sx={{ alignItems: 'center', mt: 0.5 }}>
          <Typography variant="caption" sx={{ color: 'text.secondary', flex: 1 }}>{l.member} · {l.category}</Typography>
          <StateChip state={l.state} label={l.state_label} />
        </Stack>
      </Box>)}
      {!lines.length && <Typography sx={{ color: 'text.secondary', py: 2 }}>Nothing booked here.</Typography>}
    </Stack> : <Box sx={boxStyle}>
      <Table size="small" sx={{ '& td, & th': { whiteSpace: 'nowrap', px: 1.25 } }}>
        <TableHead><TableRow>
          <TableCell sortDirection={filters.dir}>
            <TableSortLabel active direction={filters.dir}
              onClick={() => change({ dir: filters.dir === 'asc' ? 'desc' : 'asc' })}>{TIME_HEADS[0]}</TableSortLabel>
          </TableCell>
          {TIME_HEADS.slice(1).map((h) => <TableCell key={h}>{h}</TableCell>)}
          <TableCell>Member</TableCell><TableCell>Category</TableCell><TableCell>State</TableCell><TableCell>Reference</TableCell>
          <TableCell padding="checkbox" />
        </TableRow></TableHead>
        <TableBody>
          {lines.map((l) => <TableRow key={l.id} hover onClick={() => open(l.booking)} sx={{ cursor: 'pointer' }}>
            <TableCell>{l.start_date}</TableCell><TableCell>{l.start_time}</TableCell>
            <TableCell>{l.end_date}</TableCell><TableCell>{l.end_time}</TableCell>
            <TableCell>{l.member}</TableCell>
            <TableCell>{l.category}</TableCell>
            <TableCell><StateChip state={l.state} label={l.state_label} /></TableCell>
            <TableCell sx={{ fontWeight: 600 }}>
              <Link href={bookingAddress(l.booking)} onClick={go(l.booking)} underline="none" color="inherit" sx={plainLink}>{l.reference}</Link>
            </TableCell>
            <TableCell padding="checkbox"><ChevronRightRounded fontSize="small" sx={{ color: 'text.secondary' }} /></TableCell>
          </TableRow>)}
          {!lines.length && <TableRow><TableCell colSpan={9} sx={{ color: 'text.secondary', py: 4, textAlign: 'center' }}>Nothing booked here.</TableCell></TableRow>}
        </TableBody>
      </Table>
    </Box>)}
    {data && data.count > data.per_page && <TablePagination component="div" count={data.count} page={filters.page}
      rowsPerPage={data.per_page} rowsPerPageOptions={[data.per_page]}
      onPageChange={(_, page) => setFilters((f) => ({ ...f, page }))} />}
  </Stack>;
}
