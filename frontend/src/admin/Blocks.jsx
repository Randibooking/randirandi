// SPDX-License-Identifier: AGPL-3.0-only
import React from 'react';
import {
  Alert, Box, Button, Chip, CircularProgress, Link, MenuItem, Stack, Table, TableBody, TableCell, TableHead,
  TablePagination, TableRow, TableSortLabel, TextField, Typography,
} from '@mui/material';
import AddRounded from '@mui/icons-material/AddRounded';
import { Sheet } from '../book/AddSheet';
import { layBlocks, makeBlocks, readBlocks, removeBlock } from './api';
import {
  BLOCK_WHEN, blockFailure, blocksQuery, canBlock, firstStretch, layQuery, makeBody, pieceWords, stretchAsked,
} from './blockForm';
import { addDays, bookingAddress, startOrder, todayIn } from './bookings';
import { EventsInTheWay } from './EventPages';
import { blockDays } from './events';
import { Dates, Field, Progress, boxStyle, cardStyle, openBooking, plainLink } from './parts';
import useRead from './useRead';

// The Blocks: the list, New block and One block. The server lays a stretch as blocks
// (/admin/blocks/lay/) and saves them (/admin/blocks/make/); times are on each
// asset's own clock.

// The list is asked again when `_` changes (a block was made or removed).
const readList = ({ _, ...query }) => readBlocks(query);
// No preview to ask for: an answer of nothing.
const noPreview = async () => ({ ok: true, data: null });

function NewBlock({ assets, today, csrf, phone, open, onClose, onDone }) {
  const [form, setForm] = React.useState(() => ({ asset: assets[0]?.id ?? '', ...firstStretch(today), note: '' }));
  const [busy, setBusy] = React.useState(false);
  const [problem, setProblem] = React.useState(null);
  const asked = stretchAsked(form);
  const { data: laid, failed, loading } = useRead(asked ? layBlocks : noPreview, layQuery(form));
  const set = (patch) => { setForm((f) => ({ ...f, ...patch })); setProblem(null); };
  const shown = asked ? laid : null;
  const save = async () => {
    if (busy) return;
    setBusy(true);
    setProblem(null);
    const result = await makeBlocks(csrf, makeBody(form));
    if (result.kind === 'signed-out') {
      window.location.assign('/login/');
      return;
    }
    setBusy(false);
    if (result.ok) onDone();
    else setProblem(blockFailure(result));
  };
  const field = (text, name) => <TextField type="datetime-local" size="small" label={text} value={form[name]}
    onChange={(e) => e.target.value && set({ [name]: e.target.value })} slotProps={{ inputLabel: { shrink: true } }}
    sx={{ flex: 1, colorScheme: 'dark' }} />;
  return <Sheet wide={!phone} onClose={busy ? () => {} : onClose}
    title={<Typography variant="h6" sx={{ fontWeight: 600 }}>New block</Typography>}>
    <Stack spacing={2}>
      <TextField select size="small" label="Asset" value={form.asset} onChange={(e) => set({ asset: e.target.value })}>
        {assets.map((a) => <MenuItem key={a.id} value={a.id}>{a.name}</MenuItem>)}
      </TextField>
      <Stack direction={phone ? 'column' : 'row'} spacing={1.5}>{field('Start', 'start')}{field('End', 'end')}</Stack>
      <TextField size="small" label="Note" value={form.note} onChange={(e) => set({ note: e.target.value })}
        slotProps={{ htmlInput: { maxLength: 500 } }} />
      {failed && <Alert severity="error">The blocks could not be worked out.</Alert>}
      {shown && shown.pieces.length > 0 && <Box>
        <Typography variant="body2" sx={{ fontWeight: 600, mb: 0.5 }}>Blocks ({shown.pieces.length})</Typography>
        {shown.pieces.map((p) => <Typography key={`${p.container}${p.start_date}${p.start_time}`} variant="body2"
          sx={{ color: 'text.secondary' }}>{pieceWords(p)}</Typography>)}
      </Box>}
      {shown && shown.booked.length > 0 && <Box>
        <Typography variant="body2" sx={{ fontWeight: 600, color: 'warning.light', mb: 0.5 }}>Booked</Typography>
        <Stack direction="row" sx={{ flexWrap: 'wrap', gap: 1 }}>
          {shown.booked.map((n, at) => <Link key={n} href={bookingAddress(n)} variant="body2" color="inherit" underline="none"
            sx={plainLink} onClick={(e) => { openBooking(open, n)(e); if (e.defaultPrevented) onClose(); }}>Reference {shown.booked_references[at]}</Link>)}
        </Stack>
      </Box>}
      {shown && <EventsInTheWay days={blockDays(shown)} today={today} />}
      {shown && shown.blocked && <Typography variant="body2" sx={{ fontWeight: 600, color: 'warning.light' }}>Blocked already</Typography>}
      {problem && <Alert severity="error">{problem}</Alert>}
      <Stack direction="row" spacing={1} sx={{ justifyContent: 'flex-end' }}>
        <Button onClick={onClose} disabled={busy}>Cancel</Button>
        <Button variant="contained" color="error" disabled={busy || !canBlock(shown, loading)} onClick={save}
          startIcon={busy ? <CircularProgress size={16} color="inherit" /> : null}>Block</Button>
      </Stack>
    </Stack>
  </Sheet>;
}

function OneBlock({ block, csrf, phone, onClose, onDone }) {
  const [busy, setBusy] = React.useState(false);
  const [problem, setProblem] = React.useState(null);
  const remove = async () => {
    if (busy) return;
    setBusy(true);
    setProblem(null);
    const result = await removeBlock(csrf, block.id);
    if (result.kind === 'signed-out') {
      window.location.assign('/login/');
      return;
    }
    setBusy(false);
    if (result.ok || (result.kind === 'refused' && result.status === 404)) onDone();
    else setProblem(blockFailure(result));
  };
  return <Sheet wide={!phone} onClose={busy ? () => {} : onClose}
    title={<Typography variant="h6" sx={{ fontWeight: 600 }}>Block</Typography>}>
    <Stack spacing={2}>
      <Box>
        <Field label="Start">{block.start_date} {block.start_time}</Field>
        <Field label="End">{block.end_date} {block.end_time}</Field>
        <Field label="Asset">{block.asset}</Field>
        <Field label="Booking type">{block.booking_type}</Field>
        <Field label="Note">{block.note || '–'}</Field>
        <Field label="Made by">{block.made_by}</Field>
        <Field label="Created">{block.created ?? '–'}</Field>
      </Box>
      {problem && <Alert severity="error">{problem}</Alert>}
      <Stack direction="row" spacing={1} sx={{ justifyContent: 'flex-end' }}>
        <Button onClick={onClose} disabled={busy}>Close</Button>
        {block.coming && <Button variant="outlined" sx={{ color: 'error.light', borderColor: 'error.dark' }} onClick={remove}
          disabled={busy} startIcon={busy ? <CircularProgress size={16} color="inherit" /> : null}>Remove block</Button>}
      </Stack>
    </Stack>
  </Sheet>;
}

export default function Blocks({ config, phone, open, again }) {
  const today = React.useMemo(() => todayIn(config.time_zone), [config.time_zone]);
  const [filters, setFilters] = React.useState({ when: 'coming', from: today, to: addDays(today, 6), dir: 'asc', page: 0 });
  const [changed, setChanged] = React.useState(0);
  const [making, setMaking] = React.useState(false);
  const [shownId, setShownId] = React.useState(null);
  const change = (patch) => setFilters((f) => ({ ...f, page: 0, ...patch }));
  const { data, failed, loading } = useRead(readList, { ...blocksQuery(filters), _: changed }, again);
  const rows = data?.blocks ?? [];
  const block = rows.find((b) => b.id === shownId) ?? null;
  const done = () => { setMaking(false); setShownId(null); setChanged((n) => n + 1); };
  const dates = <Dates label="Blocked for" choices={BLOCK_WHEN} when={filters.when}
    setWhen={(when) => change({ when, dir: startOrder(when) })} from={filters.from} setFrom={(from) => change({ from })}
    to={filters.to} setTo={(to) => change({ to })} phone={phone} />;
  return <Stack spacing={2}>
    <Stack direction="row" sx={{ alignItems: 'center', flexWrap: 'wrap', gap: 1.5 }}>
      <Box sx={{ flex: 1 }}>{dates}</Box>
      <Button variant="contained" startIcon={<AddRounded />} disabled={!data || !data.assets.length}
        onClick={() => setMaking(true)}>New block</Button>
    </Stack>
    {failed && <Alert severity="error">The blocks could not be loaded.</Alert>}
    <Progress loading={loading} />
    {data && (phone ? <Stack spacing={1}>
      {rows.map((x) => <Box key={x.id} onClick={() => setShownId(x.id)} sx={cardStyle}>
        <Stack direction="row" spacing={1} sx={{ alignItems: 'center' }}>
          <Typography variant="body2" sx={{ fontWeight: 600, flex: 1 }}>{x.start_date} {x.start_time}</Typography>
          <Chip size="small" variant="outlined" label={x.booking_type} />
        </Stack>
        <Typography variant="body2" sx={{ color: 'text.secondary' }}>to {x.end_date} {x.end_time}</Typography>
        <Typography variant="caption" sx={{ color: 'text.secondary' }}>{x.asset}{x.note ? ` · ${x.note}` : ''}</Typography>
      </Box>)}
      {!rows.length && <Typography sx={{ color: 'text.secondary', py: 2 }}>Nothing blocked here.</Typography>}
    </Stack> : <Box sx={boxStyle}>
      <Table size="small" sx={{ '& td, & th': { whiteSpace: 'nowrap', px: 1.5 } }}>
        <TableHead><TableRow>
          <TableCell sortDirection={filters.dir}>
            <TableSortLabel active direction={filters.dir}
              onClick={() => change({ dir: filters.dir === 'asc' ? 'desc' : 'asc' })}>Start date</TableSortLabel>
          </TableCell>
          <TableCell>Start time</TableCell><TableCell>End date</TableCell><TableCell>End time</TableCell>
          <TableCell>Asset</TableCell><TableCell>Booking type</TableCell><TableCell>Note</TableCell><TableCell>Made by</TableCell>
        </TableRow></TableHead>
        <TableBody>
          {rows.map((x) => <TableRow key={x.id} hover onClick={() => setShownId(x.id)} sx={{ cursor: 'pointer' }}>
            <TableCell>{x.start_date}</TableCell><TableCell>{x.start_time}</TableCell>
            <TableCell>{x.end_date}</TableCell><TableCell>{x.end_time}</TableCell>
            <TableCell>{x.asset}</TableCell><TableCell>{x.booking_type}</TableCell>
            <TableCell sx={{ maxWidth: 220, overflow: 'hidden', textOverflow: 'ellipsis' }}>{x.note || '–'}</TableCell>
            <TableCell>{x.made_by}</TableCell>
          </TableRow>)}
          {!rows.length && <TableRow><TableCell colSpan={8} sx={{ color: 'text.secondary', py: 4, textAlign: 'center' }}>Nothing blocked here.</TableCell></TableRow>}
        </TableBody>
      </Table>
    </Box>)}
    {data && data.count > data.per_page && <TablePagination component="div" count={data.count} page={filters.page}
      rowsPerPage={data.per_page} rowsPerPageOptions={[data.per_page]}
      onPageChange={(_, page) => setFilters((f) => ({ ...f, page }))} />}
    {making && <NewBlock assets={data.assets} today={today} csrf={config.csrf} phone={phone} open={open}
      onClose={() => setMaking(false)} onDone={done} />}
    {block && <OneBlock block={block} csrf={config.csrf} phone={phone} onClose={() => setShownId(null)} onDone={done} />}
  </Stack>;
}
