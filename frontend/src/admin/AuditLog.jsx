// SPDX-License-Identifier: AGPL-3.0-only
import React from 'react';
import {
  Alert, Box, InputAdornment, Link, MenuItem, Stack, Table, TableBody, TableCell, TableHead, TablePagination,
  TableRow, TableSortLabel, TextField, Typography, useMediaQuery,
} from '@mui/material';
import { useTheme } from '@mui/material/styles';
import SearchRounded from '@mui/icons-material/SearchRounded';
import { readAuditEvents } from './api';
import { WHEN, addDays, auditQuery, todayIn } from './audit';
import { targetAddress } from './auditLinks';
import { bookingAddress } from './bookings';
import { Progress } from './parts';
import { plainClick } from './route';
import useRead from './useRead';

// The Audit log: every
// administrator action, as the server answers it in pages (/admin/audit/events/);
// nothing here is changed or removed. What an event acted on links to its page or
// place where one exists (`what.booking`, `what.target`).
const box = { border: 1, borderColor: 'divider', borderRadius: 2, overflowX: 'auto', bgcolor: 'background.paper' };
const tight = { '& td, & th': { whiteSpace: 'nowrap', px: 1.25 } };
const phoneCard = { border: 1, borderColor: 'divider', borderRadius: 2, p: 1.5, bgcolor: 'background.paper' };

function Dates({ when, setWhen, from, setFrom, to, setTo, phone }) {
  const field = (text, value, set) => <TextField type="date" size="small" label={text} value={value}
    onChange={(e) => e.target.value && set(e.target.value)} slotProps={{ inputLabel: { shrink: true } }}
    sx={{ width: phone ? 'auto' : 160, flex: phone ? 1 : 'none', colorScheme: 'dark' }} />;
  return <Stack direction="row" spacing={1.5} sx={{ alignItems: 'center' }}>
    <TextField select size="small" label="When" value={when} onChange={(e) => setWhen(e.target.value)}
      sx={{ width: 150, flexShrink: 0 }}>
      {WHEN.map(([k, l]) => <MenuItem key={k} value={k}>{l}</MenuItem>)}
    </TextField>
    {when === 'day' && field('Date', from, setFrom)}
    {when === 'period' && <>{field('From', from, setFrom)}{field('To', to, setTo)}</>}
  </Stack>;
}

// What an event acted on, a link where a page or a place exists for it: a click
// opens it in the page, a click with a key held opens the address.
function What({ what, openWhat }) {
  const href = what.booking ? bookingAddress(what.booking) : targetAddress(what.target);
  return <><Typography component="span" variant="body2" sx={{ color: 'text.secondary', mr: 0.75 }}>{what.kind}</Typography>
    {href
      ? <Link href={href} variant="body2" underline="hover" color="inherit"
        onClick={(event) => { if (plainClick(event)) { event.preventDefault(); openWhat(what); } }}>{what.name}</Link>
      : <Typography component="span" variant="body2">{what.name}</Typography>}</>;
}

export default function AuditLog({ config, again, openWhat }) {
  const phone = !useMediaQuery(useTheme().breakpoints.up('md'), { noSsr: true });
  const today = React.useMemo(() => todayIn(config.time_zone), [config.time_zone]);
  const [filters, setFilters] = React.useState({
    when: 'all', from: addDays(today, -6), to: today, by: 'all', action: 'all', q: '', dir: 'desc', page: 0,
  });
  // The filters change the page; a new filter starts on the first.
  const change = (patch) => setFilters((f) => ({ ...f, page: 0, ...patch }));
  const { data, failed, loading } = useRead(readAuditEvents, auditQuery(filters), again);
  const events = data?.events ?? [];
  const choose = (label, name, all, options) => <TextField select size="small" label={label} value={filters[name]}
    onChange={(e) => change({ [name]: e.target.value })} sx={{ minWidth: phone ? 0 : 170, flex: phone ? 1 : 'none' }}>
    <MenuItem value="all">{all}</MenuItem>
    {options.map(([value, text]) => <MenuItem key={value} value={value}>{text}</MenuItem>)}
  </TextField>;
  const admin = choose('Administrator', 'by', 'Everyone', (data?.administrators ?? []).map((a) => [String(a.id), a.name]));
  const action = choose('Action', 'action', 'Every action', (data?.actions ?? []).map((a) => [a.value, a.label]));
  const search = <TextField size="small" placeholder="Booking, member, container, event" value={filters.q}
    onChange={(e) => change({ q: e.target.value })} sx={{ flex: 1, maxWidth: phone ? 'none' : 260, minWidth: 160 }}
    slotProps={{ input: { startAdornment: <InputAdornment position="start"><SearchRounded fontSize="small" /></InputAdornment> } }} />;
  const dates = <Dates when={filters.when} setWhen={(when) => change({ when })} from={filters.from} setFrom={(from) => change({ from })}
    to={filters.to} setTo={(to) => change({ to })} phone={phone} />;
  return <Stack spacing={2}>
    {phone ? <Stack spacing={1.5}>{dates}<Stack direction="row" spacing={1}>{admin}{action}</Stack>
      <Box sx={{ display: 'flex' }}>{search}</Box></Stack>
      : <Stack direction="row" sx={{ flexWrap: 'wrap', gap: 1.5, alignItems: 'center' }}>{dates}{admin}{action}{search}</Stack>}
    {failed && <Alert severity="error">The audit log could not be loaded.</Alert>}
    <Progress loading={loading} />
    {data && (phone ? <Stack spacing={1}>
      {events.map((e) => <Box key={e.id} sx={phoneCard}>
        <Typography variant="body2" sx={{ fontWeight: 600 }}>{e.action_label}</Typography>
        <Box><What what={e.what} openWhat={openWhat} /></Box>
        <Typography variant="caption" sx={{ color: 'text.secondary' }}>{e.administrator.name} · {e.at}</Typography>
      </Box>)}
      {!events.length && <Typography sx={{ color: 'text.secondary', py: 2 }}>None</Typography>}
    </Stack> : <Box sx={box}>
      <Table size="small" sx={tight}>
        <TableHead><TableRow>
          <TableCell sortDirection={filters.dir}>
            <TableSortLabel active direction={filters.dir} onClick={() => setFilters((f) => ({ ...f, dir: f.dir === 'asc' ? 'desc' : 'asc' }))}>When</TableSortLabel>
          </TableCell>
          <TableCell>Administrator</TableCell><TableCell>Action</TableCell><TableCell>What</TableCell>
        </TableRow></TableHead>
        <TableBody>
          {events.map((e) => <TableRow key={e.id}>
            <TableCell sx={{ color: 'text.secondary' }}>{e.at}</TableCell>
            <TableCell>{e.administrator.name}</TableCell>
            <TableCell>{e.action_label}</TableCell>
            <TableCell><What what={e.what} openWhat={openWhat} /></TableCell>
          </TableRow>)}
          {!events.length && <TableRow><TableCell colSpan={4} sx={{ color: 'text.secondary', py: 4, textAlign: 'center' }}>None</TableCell></TableRow>}
        </TableBody>
      </Table>
    </Box>)}
    {data && data.count > data.per_page && <TablePagination component="div" count={data.count} page={filters.page}
      rowsPerPage={data.per_page} rowsPerPageOptions={[data.per_page]}
      onPageChange={(_, page) => setFilters((f) => ({ ...f, page }))} />}
  </Stack>;
}
