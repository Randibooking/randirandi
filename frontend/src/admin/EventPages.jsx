// SPDX-License-Identifier: AGPL-3.0-only
import React from 'react';
import {
  Alert, Autocomplete, Box, Button, Checkbox, Chip, FormControlLabel, IconButton, InputAdornment, Menu, MenuItem, Stack, Switch,
  Table, TableBody, TableCell, TableHead, TablePagination, TableRow, TableSortLabel, TextField, Typography,
} from '@mui/material';
import AddRounded from '@mui/icons-material/AddRounded';
import ArrowBackRounded from '@mui/icons-material/ArrowBackRounded';
import MoreHorizRounded from '@mui/icons-material/MoreHorizRounded';
import SearchRounded from '@mui/icons-material/SearchRounded';
import { Sheet } from '../book/AddSheet';
import {
  changeEvent, eventDayHours, makeEvent, previewEvent, readContacts, readEvent, readEvents, removeEvent, removeEventDay,
} from './api';

import { addDays, bookingAddress, startOrder, todayIn } from './bookings';
import { useCrumb } from './crumbs';
import {
  EVENT_WHEN, EVENT_WORDS, REASON_WORDS, SHOWN_SKIPS, WEEKDAYS, asked, categoryWord, comingDays, dayKey, dayWords, daysShown, daysWords, draft,
  EVENTS_ADDRESS, eventAddress, eventBody, eventFailure, eventOfAddress, eventsQuery, formChanged, hours, hoursClear, leftWords, longDay, otherHoursFor, previewBody, saveable, seeWords,
  skipWhy, timeChanged, timeWords, toldWords, whenOf, PER_PAGE,
} from './events';
import { Card, Dates, Field, OpenCard, OpenRow, Progress, boxStyle } from './parts';
import { Problems, SheetButtons, Toast, useKept } from './setupParts';
import useRead from './useRead';

// Admin's events , as the owner approved their drawing
// (25 October 2026, index ): a place in Daily after Blocks, the list with
// the Filters of Blocks, a sheet to make an event with its preview, and a page
// per event with sections to change, as a container has. The server
// (/admin/events/) keeps every rule and works the preview out.

const tight = { '& td, & th': { whiteSpace: 'nowrap', px: 1.25 } };
const Title = ({ children }) => <Typography variant="h6" sx={{ fontWeight: 600 }}>{children}</Typography>;
// No read to make: an answer of nothing.
const noRead = async () => ({ ok: true, data: null });
const readList = ({ _, ...query }) => readEvents(query);
const readOne = ({ id }) => (id === null ? noRead() : readEvent(id));

function whenWords(e, today) {
  return e.start ? timeWords(e.start, e.end, today) : '';
}
const NoContact = () => <Chip size="small" variant="outlined" color="warning" label="No contact" />;

// One call at a time: the answer closes the sheet, a refusal stays on it.
function useRun(done) {
  const [busy, setBusy] = React.useState(false);
  const [problem, setProblem] = React.useState(null);
  const running = React.useRef(false);
  const run = async (call, toast) => {
    if (running.current) return;
    running.current = true;
    setBusy(true);
    setProblem(null);
    const result = await call();
    if (result.kind === 'signed-out') {
      window.location.assign('/login/');
      return;
    }
    running.current = false;
    setBusy(false);
    if (result.ok) done(result.data, toast);
    else setProblem(eventFailure(result));
  };
  return { busy, problem, run, clear: () => setProblem(null) };
}

// --- The form of an event: every field of S1 and the repeat (A2).

function Choice({ label: text, value, set, options, disabled }) {
  return <TextField select size="small" label={text} value={value} disabled={disabled} onChange={(e) => set(e.target.value)}>
    {options.map(([v, words]) => <MenuItem key={String(v)} value={v}>{words}</MenuItem>)}
  </TextField>;
}

function Days({ value, set }) {
  return <Stack direction="row" sx={{ flexWrap: 'wrap', gap: 0.5 }}>
    {WEEKDAYS.map((d, i) => <Chip key={d} label={d} size="small" variant={value.includes(i) ? 'filled' : 'outlined'}
      // Scheme C's chosen pair (action.chosen, action.chosenText).
      sx={value.includes(i) ? { bgcolor: '#eeeeec', '& .MuiChip-label': { color: '#111110', fontWeight: 600 }, '&:hover': { bgcolor: '#b5b3ad' } } : {}}
      onClick={() => set(value.includes(i) ? value.filter((x) => x !== i) : [...value, i].sort((a, b) => a - b))} />)}
  </Stack>;
}

const moment = (text, value, set) => <TextField type="datetime-local" size="small" label={text} value={value}
  onChange={(e) => e.target.value && set(e.target.value)} slotProps={{ inputLabel: { shrink: true } }}
  sx={{ flex: 1, colorScheme: 'dark', minWidth: 0 }} />;

function DetailFields({ f, set, options }) {
  return <>
    <Choice label="Category" value={f.category} set={(category) => set({ category })} options={options.categories.map((c) => [c.key, c.label])} />
    <Choice label="Asset" value={f.asset} set={(asset) => set({ asset, resolved: {} })}
      options={[['', 'None'], ...options.assets.map((a) => [a.id, a.name])]} />
    <TextField size="small" label="Title" value={f.title} onChange={(e) => set({ title: e.target.value })} slotProps={{ htmlInput: { maxLength: 200 } }} />
    <TextField size="small" label="Description" value={f.description} multiline minRows={2}
      onChange={(e) => set({ description: e.target.value })} slotProps={{ htmlInput: { maxLength: 2000 } }} />
  </>;
}

function TimeFields({ f, set, phone }) {
  const end = f.until ? 'date' : 'none';
  return <>
    <Stack direction={phone ? 'column' : 'row'} spacing={1.5}>
      {moment('Starts', f.start, (start) => set({ start, end: f.end <= start ? start : f.end, resolved: {} }))}
      {moment('Ends', f.end, (end_) => set({ end: end_, resolved: {} }))}
    </Stack>
    <FormControlLabel control={<Switch checked={f.repeat}
      onChange={(e) => set({ repeat: e.target.checked, weekdays: e.target.checked && !f.weekdays.length ? [new Date(`${f.start.slice(0, 10)}T00:00Z`).getUTCDay() === 0 ? 6 : new Date(`${f.start.slice(0, 10)}T00:00Z`).getUTCDay() - 1] : f.weekdays, resolved: {} })} />}
      label="Repeats weekly" />
    {f.repeat && <Stack spacing={2}>
      <Days value={f.weekdays} set={(weekdays) => set({ weekdays, resolved: {} })} />
      <Stack direction="row" spacing={1.5}>
        <TextField select size="small" label="Until" value={end} sx={{ width: 140 }}
          onChange={(e) => set({ until: e.target.value === 'none' ? '' : addDays(f.start.slice(0, 10), 56) })}>
          <MenuItem value="none">No end</MenuItem><MenuItem value="date">A date</MenuItem>
        </TextField>
        {end === 'date' && <TextField type="date" size="small" label="Last date" value={f.until}
          onChange={(e) => e.target.value && set({ until: e.target.value })} slotProps={{ inputLabel: { shrink: true } }}
          sx={{ flex: 1, colorScheme: 'dark' }} />}
      </Stack>
    </Stack>}
  </>;
}

// The contact person: an active member found by name on the server.
function ContactPicker({ value, set }) {
  const [text, setText] = React.useState('');
  const { data } = useRead(readContacts, { q: text });
  const people = data?.people ?? [];
  const options = value && !people.some((p) => p.id === value.id) ? [value, ...people] : people;
  return <Autocomplete size="small" options={options} value={value} filterOptions={(all) => all}
    getOptionLabel={(o) => o.name} isOptionEqualToValue={(a, b) => a.id === b.id}
    onChange={(_, chosen) => set(chosen)}
    onInputChange={(_, input, reason) => { if (reason === 'input') setText(input); }}
    renderInput={(params) => <TextField {...params} label="Contact person" />} />;
}

function ContactFields({ f, set }) {
  return <>
    <ContactPicker value={f.person} set={(person) => set({ person })} />
    {f.person && <Stack direction="row" sx={{ alignItems: 'center', flexWrap: 'wrap' }}>
      <Typography variant="body2" sx={{ color: 'text.secondary', mr: 1.5 }}>Shown</Typography>
      <FormControlLabel control={<Checkbox size="small" checked disabled />} label="Name" />
      <FormControlLabel control={<Checkbox size="small" checked={f.email} onChange={(e) => set({ email: e.target.checked })} />} label="Email" />
      <FormControlLabel control={<Checkbox size="small" checked={f.phone} onChange={(e) => set({ phone: e.target.checked })} />} label="Phone" />
    </Stack>}
  </>;
}

// --- The preview of S5, before saving, with its ways out (A3).

function QuarterSelect({ label: text, value, set, error = false }) {
  const options = Array.from({ length: 96 }, (_, i) => `${String(Math.floor(i / 4)).padStart(2, '0')}:${String((i % 4) * 15).padStart(2, '0')}`);
  if (!options.includes(value)) options.push(value);
  options.sort();
  return <TextField select size="small" label={text} value={value} error={error} onChange={(e) => set(e.target.value)} sx={{ width: 100 }}
    slotProps={{ select: { MenuProps: { slotProps: { paper: { sx: { maxHeight: 280 } } } } } }}>
    {options.map((m) => <MenuItem key={m} value={m}>{m}</MenuItem>)}
  </TextField>;
}

const wayBox = { px: 1.5, py: 1, borderTop: 1, borderColor: 'divider', '&:first-of-type': { borderTop: 0 } };

function WayItem({ w, f, set, today, open, phone, fixed }) {
  const resolved = f.resolved[w.day];
  const clear = hoursClear(w, resolved, f);
  const resolve = (r) => set({ resolved: { ...f.resolved, [w.day]: r } });
  if (w.reason === 'booked') {
    return <Stack direction={phone ? 'column' : 'row'} spacing={1} sx={{ ...wayBox, alignItems: phone ? 'flex-start' : 'center' }}>
      <Box sx={{ flex: 1 }}>
        <Typography variant="body2" sx={{ fontWeight: 600 }}>Booked · Reference {w.reference}</Typography>
        {w.start && <Typography variant="caption" sx={{ color: 'text.secondary' }}>
          {dayWords(w.start.slice(0, 10), today)} {hours(w.start.slice(11), w.end.slice(11))} · {w.member}</Typography>}
      </Box>
      <Button size="small" href={bookingAddress(w.number)} onClick={(e) => { if (open) { e.preventDefault(); open(w.number); } }}>Open booking</Button>
    </Stack>;
  }
  if (w.reason === 'closed') {
    return <Box sx={wayBox}>
      <Typography variant="body2" sx={{ fontWeight: 600 }}>Closed{w.date ? ` · ${dayWords(w.date, today)}` : ''}</Typography>
    </Box>;
  }
  if (w.reason === 'blocked') {
    return <Box sx={wayBox}>
      <Typography variant="body2" sx={{ fontWeight: 600 }}>Blocked{w.start ? ` · ${timeWords(w.start, w.end, today)}` : ''}</Typography>
      {w.note && <Typography variant="caption" sx={{ color: 'text.secondary' }}>{w.note}</Typography>}
    </Box>;
  }
  const struck = resolved === 'remove' ? { textDecoration: 'line-through' } : {};
  return <Box sx={wayBox}>
    <Stack direction={phone ? 'column' : 'row'} spacing={1} sx={{ alignItems: phone ? 'flex-start' : 'center' }}>
      <Box sx={{ flex: 1, opacity: resolved === 'remove' ? 0.6 : 1 }}>
        <Typography variant="body2" sx={{ fontWeight: 600, ...struck }}>{w.title}</Typography>
        {w.start && <Typography variant="caption" sx={{ color: 'text.secondary', ...struck }}>{timeWords(w.start, w.end, today)}</Typography>}
      </Box>
      {resolved
        ? <Stack direction="row" spacing={1} sx={{ alignItems: 'center' }}>
          <Chip size="small" variant="outlined" label={resolved === 'remove' ? 'Day removed' : hours(resolved.from, resolved.to)} />
          <Button size="small" onClick={() => resolve(undefined)}>Undo</Button>
        </Stack>
        : w.repeating && !fixed && <Stack direction="row" spacing={0.5}>
          <Button size="small" color="error" onClick={() => resolve('remove')}>Remove that day</Button>
          <Button size="small" onClick={() => resolve(otherHoursFor(w, f))}>Other hours</Button>
        </Stack>}
    </Stack>
    {resolved && resolved !== 'remove' && <Stack direction="row" spacing={1} sx={{ mt: 1, alignItems: 'center' }}>
      <QuarterSelect label="From" value={resolved.from} error={!clear} set={(from) => resolve({ ...resolved, from })} />
      <QuarterSelect label="To" value={resolved.to} error={!clear} set={(to) => resolve({ ...resolved, to })} />
      {!clear && <Typography variant="caption" sx={{ color: 'error.light' }}>Still in the way</Typography>}
    </Stack>}
  </Box>;
}

// `fixed`: no ways out offered here (a day's own sheet, whose Save waits).
function Preview({ shown, f, set, today, open, phone, assetName, fixed = false }) {
  const [all, setAll] = React.useState(false);
  if (!shown) return null;
  const heading = (text) => <Typography variant="body2" sx={{ fontWeight: 600, mb: 0.5 }}>{text}</Typography>;
  const list = { border: 1, borderColor: 'divider', borderRadius: 1.5, maxHeight: 260, overflowY: 'auto' };
  if (shown.repeat) {
    const skipped = shown.skipped;
    const skips = all ? skipped : skipped.slice(0, SHOWN_SKIPS);
    return <Stack spacing={1.5}>
      <Box>
        {heading('Takes')}
        <Typography variant="body2">{shown.takes.length} days · {assetName}</Typography>
        <Typography variant="caption" sx={{ color: 'text.secondary' }}>
          {daysWords(f.weekdays)} {hours(whenOf(f).from, whenOf(f).to)}, {dayWords(f.start.slice(0, 10), today)} to {f.until ? dayWords(f.until, today) : 'no end'}</Typography>
      </Box>
      {shown.left.length > 0 && <Box>
        {heading('Left over')}
        {shown.left.map((x) => <Typography key={x[0]} variant="body2" sx={{ color: 'text.secondary' }}>Each day {leftWords(x, today)}</Typography>)}
      </Box>}
      {skipped.length > 0 && <Box>
        {heading(`Skipped (${skipped.length})`)}
        <Stack direction="row" sx={{ flexWrap: 'wrap', gap: 0.75, mb: 1 }}>
          {Object.entries(shown.counts).map(([reason, n]) => <Chip key={reason} size="small" variant="outlined" color="warning" label={`${REASON_WORDS[reason]} ${n}`} />)}
        </Stack>
        <Box sx={list}>
          {skips.map((s, i) => <Stack key={s.date} direction="row" spacing={1} sx={{ px: 1.5, py: 0.75, borderTop: i ? 1 : 0, borderColor: 'divider' }}>
            <Typography variant="body2" sx={{ width: 110, flexShrink: 0 }}>{dayWords(s.date, today)}</Typography>
            <Typography variant="body2" sx={{ color: 'text.secondary' }}>{skipWhy(s)}</Typography>
          </Stack>)}
        </Box>
        {skipped.length > SHOWN_SKIPS && <Button size="small" onClick={() => setAll(!all)} sx={{ mt: 0.5 }}>{all ? 'Show fewer' : `Show all ${skipped.length}`}</Button>}
      </Box>}
    </Stack>;
  }
  const date = shown.start.slice(0, 10);
  const same = shown.end.slice(0, 10) === date;
  return <Stack spacing={1.5}>
    <Box>
      {heading('Takes')}
      <Typography variant="body2">{assetName} · {longDay(date)}, {same ? hours(shown.start.slice(11), shown.end.slice(11))
        : `${shown.start.slice(11)} – ${dayWords(shown.end.slice(0, 10), today)} ${shown.end.slice(11)}`}</Typography>
    </Box>
    {shown.left.length > 0 && <Box>
      {heading('Left over')}
      {shown.left.map((x) => <Typography key={x[0]} variant="body2" sx={{ color: 'text.secondary' }}>{leftWords(x, today)}</Typography>)}
    </Box>}
    {shown.way.length > 0 && <Box>
      {heading('In the way')}
      <Box sx={list}>{shown.way.map((w, i) => <WayItem key={`${w.reason}${w.day ?? w.number ?? w.date ?? ''}${i}`} w={w} f={f} set={set} today={today}
        open={open} phone={phone} fixed={fixed} />)}</Box>
    </Box>}
  </Stack>;
}

// The preview of a form, asked of the server when the asset, the time or a way
// out changes. `shown` is null while there is none to show or it is on its way.
function usePreview(f, eventId, csrf, wanted) {
  const read = React.useCallback((body) => previewEvent(csrf, body), [csrf]);
  const { data, loading, failed } = useRead(wanted ? read : noRead, previewBody(f, eventId));
  return { shown: wanted && !loading ? data : null, loading: wanted && loading, failed: wanted && failed };
}

// --- One sheet for making an event, and one per section of its page.

function EventSheet({ title, e = null, parts, today, phone, close, done, open, options, csrf }) {
  const [was] = React.useState(() => draft(e, today));
  const [f, setF] = React.useState(was);
  const set = (patch) => setF((before) => ({ ...before, ...patch }));
  const timed = !e || timeChanged(f, was);
  const wanted = timed && asked(f);
  const { shown, loading, failed } = usePreview(f, e?.id ?? null, csrf, wanted);
  const needContact = parts.includes('contact') || !f.person;
  const ok = saveable(f, wanted ? shown : null, { timed, contact: true }) && !loading && !failed
    && (!e || formChanged(f, was));
  const assetName = options.assets.find((a) => a.id === f.asset)?.name ?? '';
  const { busy, problem, run, clear } = useRun(done);
  const save = () => {
    if (!ok) return;
    const body = eventBody(f);
    run(() => (e ? changeEvent(csrf, e.id, body) : makeEvent(csrf, body)), 'Saved.');
  };
  return <Sheet wide={!phone} onClose={busy ? () => {} : close} title={<Title>{title}</Title>}>
    <Stack spacing={2} component="form" onSubmit={(ev) => { ev.preventDefault(); save(); }}>
      {parts.includes('details') && <DetailFields f={f} set={(p) => { clear(); set(p); }} options={options} />}
      {parts.includes('time') && <TimeFields f={f} set={(p) => { clear(); set(p); }} phone={phone} />}
      {needContact && <ContactFields f={f} set={(p) => { clear(); set(p); }} />}
      {failed && <Alert severity="error" variant="outlined">The preview could not be worked out.</Alert>}
      {wanted && <Preview key={`${f.asset}${f.start}${f.end}${f.repeat}`} shown={shown} f={f} set={(p) => { clear(); set(p); }}
        today={today} open={open} phone={phone} assetName={assetName} />}
      <Problems problem={problem} />
      <SheetButtons close={close} save={save} ok={ok} busy={busy} />
    </Stack>
  </Sheet>;
}

// A day of a repeat given other hours, or removed (A4).
function DaySheet({ e, day, kind, today, phone, close, done, csrf }) {
  const [from, setFrom] = React.useState(day.from);
  const [to, setTo] = React.useState(day.to);
  const f = { ...draft(e, today), start: `${day.date}T${from}`, end: `${day.date}T${to}`, repeat: false, asset: e.asset?.id ?? '' };
  const wanted = kind === 'hours' && Boolean(e.asset) && from < to;
  const { shown, loading, failed } = usePreview(f, e.id, csrf, wanted);
  const clear = !shown || shown.way.length === 0;
  const ok = kind === 'remove' || (from < to && clear && !loading && !failed && (from !== day.from || to !== day.to));
  const { busy, problem, run } = useRun(done);
  const save = () => {
    if (!ok) return;
    if (kind === 'remove') run(() => removeEventDay(csrf, day.id), 'Day removed.');
    else run(() => eventDayHours(csrf, day.id, { from, to }), 'Saved.');
  };
  return <Sheet wide={!phone} onClose={busy ? () => {} : close} title={<Title>{kind === 'remove' ? `Remove ${dayWords(day.date, today)}?` : `Other hours · ${dayWords(day.date, today)}`}</Title>}>
    <Stack spacing={2} component="form" onSubmit={(ev) => { ev.preventDefault(); save(); }}>
      {kind === 'remove' ? <Field label={e.title}>{dayWords(day.date, today)} {hours(day.from, day.to)}</Field>
        : <Stack direction="row" spacing={1.5}>
          <QuarterSelect label="From" value={from} set={setFrom} />
          <QuarterSelect label="To" value={to} set={setTo} />
        </Stack>}
      {wanted && <Preview shown={shown} f={f} set={() => {}} today={today} phone={phone} assetName={e.asset.name} fixed />}
      {failed && <Alert severity="error" variant="outlined">The preview could not be worked out.</Alert>}
      <Problems problem={problem} />
      <SheetButtons close={close} save={save} ok={ok} busy={busy} label={kind === 'remove' ? 'Remove' : 'Save'} />
    </Stack>
  </Sheet>;
}

function RemoveEventSheet({ e, today, phone, close, done, csrf }) {
  const days = e.repeat ? comingDays(e).length : 0;
  const { busy, problem, run } = useRun(done);
  return <Sheet wide={!phone} onClose={busy ? () => {} : close} title={<Title>Remove {e.title}?</Title>}>
    <Stack spacing={2}>
      <Box>
        <Field label="When">{e.repeat ? `${daysWords(e.repeat.weekdays)} ${hours(e.repeat.from, e.repeat.to)}` : whenWords(e, today)}</Field>
        {e.asset && <Field label="Frees">{e.repeat ? `${days} coming days on ${e.asset.name}` : e.asset.name}</Field>}
      </Box>
      <Problems problem={problem} />
      <Stack direction="row" spacing={1} sx={{ justifyContent: 'flex-end' }}>
        <Button onClick={close} disabled={busy}>Cancel</Button>
        <Button variant="contained" color="error" disabled={busy} onClick={() => run(() => removeEvent(csrf, e.id), 'Removed.')}>Remove</Button>
      </Stack>
    </Stack>
  </Sheet>;
}

// --- An event's page: sections that each open their own sheet (A4, A5).

function DayActions({ phone, day, act }) {
  const [anchor, setAnchor] = React.useState(null);
  if (day.removed) return <Chip size="small" variant="outlined" label="Removed" />;
  if (!phone) {
    return <Stack direction="row" spacing={0.5} sx={{ justifyContent: 'flex-end' }}>
      <Button size="small" onClick={() => act('hours', day)}>Other hours</Button>
      <Button size="small" color="error" onClick={() => act('remove', day)}>Remove</Button>
    </Stack>;
  }
  return <>
    <IconButton size="small" aria-label={`${day.date} actions`} onClick={(ev) => setAnchor(ev.currentTarget)}><MoreHorizRounded fontSize="small" /></IconButton>
    <Menu anchorEl={anchor} open={!!anchor} onClose={() => setAnchor(null)}>
      <MenuItem onClick={() => { setAnchor(null); act('hours', day); }}>Other hours</MenuItem>
      <MenuItem onClick={() => { setAnchor(null); act('remove', day); }} sx={{ color: 'error.light' }}>Remove</MenuItem>
    </Menu>
  </>;
}

function DaysCard({ e, today, phone, act }) {
  const [when, setWhen] = React.useState('coming');
  const [page, setPage] = React.useState(0);
  const days = daysShown(e.days, when);
  const shown = days.slice(page * PER_PAGE, (page + 1) * PER_PAGE);
  const pick = <TextField select size="small" value={when} onChange={(ev) => { setWhen(ev.target.value); setPage(0); }} sx={{ width: 120 }}
    slotProps={{ htmlInput: { 'aria-label': 'Days' } }}>
    <MenuItem value="coming">Coming</MenuItem><MenuItem value="past">Past</MenuItem>
  </TextField>;
  const line = { px: 2, py: 1, borderTop: 1, borderColor: 'divider', alignItems: 'center' };
  return <Card flush title={`Days (${days.length})`} action={<Box sx={{ pr: 2, pt: 1.5 }}>{pick}</Box>}>
    {phone ? <Box>
      {shown.map((d) => <Stack key={d.date} direction="row" spacing={1} sx={line}>
        <Box sx={{ flex: 1 }}>
          <Typography variant="body2" sx={{ fontWeight: 600, color: d.removed ? 'text.secondary' : 'text.primary' }}>{dayWords(d.date, today)}</Typography>
          <Typography variant="caption" sx={{ color: 'text.secondary' }}>{d.removed ? 'Removed' : `${hours(d.from, d.to)}${d.other ? ' · Other hours' : ''}`}</Typography>
        </Box>
        {when === 'coming' && !d.removed && <DayActions phone day={d} act={act} />}
      </Stack>)}
      {!shown.length && <Typography variant="body2" sx={{ color: 'text.secondary', px: 2, pb: 2 }}>None</Typography>}
    </Box> : <Table size="small" sx={tight}>
      <TableHead><TableRow><TableCell>Date</TableCell><TableCell>Hours</TableCell><TableCell align="right" /></TableRow></TableHead>
      <TableBody>
        {shown.map((d) => <TableRow key={d.date}>
          <TableCell sx={{ fontWeight: 600, opacity: d.removed ? 0.6 : 1 }}>{dayWords(d.date, today)}</TableCell>
          <TableCell sx={{ opacity: d.removed ? 0.6 : 1 }}>{d.removed ? '–' : <>{hours(d.from, d.to)}{d.other && <Chip size="small" variant="outlined" color="info" label="Other hours" sx={{ ml: 1 }} />}</>}</TableCell>
          <TableCell align="right">{when === 'coming' ? <DayActions phone={phone} day={d} act={act} /> : d.removed && <Chip size="small" variant="outlined" label="Removed" />}</TableCell>
        </TableRow>)}
        {!shown.length && <TableRow><TableCell colSpan={3} sx={{ color: 'text.secondary', py: 3, textAlign: 'center' }}>None</TableCell></TableRow>}
      </TableBody>
    </Table>}
    {days.length > PER_PAGE && <TablePagination component="div" count={days.length} page={page} rowsPerPage={PER_PAGE}
      rowsPerPageOptions={[PER_PAGE]} onPageChange={(_, p) => setPage(p)} />}
  </Card>;
}

function SkippedCard({ e, today, phone }) {
  const [page, setPage] = React.useState(0);
  const shown = e.skipped.slice(page * PER_PAGE, (page + 1) * PER_PAGE);
  return <Card flush title={`Skipped (${e.skipped.length})`}>
    {phone ? shown.map((s) => <Box key={s.date} sx={{ px: 2, py: 1, borderTop: 1, borderColor: 'divider' }}>
      <Typography variant="body2" sx={{ fontWeight: 600 }}>{dayWords(s.date, today)} · {skipWhy(s)}</Typography>
      <Typography variant="caption" sx={{ color: 'text.secondary' }}>{toldWords(s, e.made_by, today)}</Typography>
    </Box>) : <Table size="small" sx={tight}>
      <TableHead><TableRow><TableCell>Date</TableCell><TableCell>Why</TableCell><TableCell>Told</TableCell></TableRow></TableHead>
      <TableBody>
        {shown.map((s) => <TableRow key={s.date}>
          <TableCell sx={{ fontWeight: 600 }}>{dayWords(s.date, today)}</TableCell>
          <TableCell>{skipWhy(s)}</TableCell>
          <TableCell sx={{ color: 'text.secondary' }}>{toldWords(s, e.made_by, today)}</TableCell>
        </TableRow>)}
      </TableBody>
    </Table>}
    {e.skipped.length > PER_PAGE && <Box><TablePagination component="div" count={e.skipped.length} page={page} rowsPerPage={PER_PAGE}
      rowsPerPageOptions={[PER_PAGE]} onPageChange={(_, p) => setPage(p)} /></Box>}
  </Card>;
}

function EventPage({ e, today, phone, back, open, said, csrf, changed }) {
  const [sheet, setSheet] = React.useState(null);
  const close = () => setSheet(null);
  const done = (data, toast) => { close(); changed(data); said(toast); };
  const change = (part) => <Button size="small" onClick={() => setSheet({ kind: part })}>Change</Button>;
  const remove = <Button color="error" onClick={() => setSheet({ kind: 'remove' })}>Remove event</Button>;
  const sheetProps = { e, today, phone, close, open, csrf, options: e };
  const time = e.repeat ? `${daysWords(e.repeat.weekdays)} ${hours(e.repeat.from, e.repeat.to)}` : whenWords(e, today);
  return <Stack spacing={2}>
    {phone && <Button startIcon={<ArrowBackRounded />} onClick={back} sx={{ alignSelf: 'flex-start' }}>Events</Button>}
    <Stack direction="row" spacing={1} sx={{ alignItems: 'center', flexWrap: 'wrap', rowGap: 1 }}>
      <Typography component="h1" variant="h5" sx={{ fontWeight: 600 }}>{e.title}</Typography>
      {e.contact === null && <NoContact />}
      <Box sx={{ flex: 1 }} />
      {!phone && remove}
    </Stack>
    <Typography variant="body2" sx={{ color: 'text.secondary', mt: '4px !important' }}>
      {categoryWord(e.categories, e.category)} · {e.asset ? e.asset.name : 'No asset'} · {time}</Typography>
    <Box sx={{ display: 'grid', gap: 2, gridTemplateColumns: phone ? '1fr' : 'repeat(3, minmax(0, 1fr))' }}>
      <Card title="Details" action={change('details')}>
        <Field label="Category">{categoryWord(e.categories, e.category)}</Field>
        <Field label="Asset">{e.asset ? e.asset.name : 'None'}</Field>
        <Field label="Description">{e.description || '–'}</Field>
      </Card>
      <Card title="Time" action={change('time')}>
        {e.repeat ? <>
          <Field label="Repeats">Every {daysWords(e.repeat.weekdays)}</Field>
          <Field label="Hours">{hours(e.repeat.from, e.repeat.to)}</Field>
          <Field label="From">{longDay(e.repeat.first)}</Field>
          <Field label="Until">{e.repeat.end ? longDay(e.repeat.end) : 'No end'}</Field>
        </> : <>
          <Field label="Starts">{longDay(e.start.slice(0, 10))}, {e.start.slice(11)}</Field>
          <Field label="Ends">{longDay(e.end.slice(0, 10))}, {e.end.slice(11)}</Field>
        </>}
      </Card>
      <Card title="Contact" action={change('contact')}>
        <Field label="Person">{e.contact ? e.contact.name : 'None'}</Field>
        {e.contact && <Field label="Shown">{seeWords(e)}</Field>}
        <Field label="Made by">{e.made_by}</Field>
        <Field label="Created">{longDay(e.created)}</Field>
      </Card>
    </Box>
    {phone && <Box>{remove}</Box>}
    {e.repeat && <DaysCard e={e} today={today} phone={phone} act={(kind, day) => setSheet({ kind, day })} />}
    {e.repeat && e.skipped.length > 0 && <SkippedCard e={e} today={today} phone={phone} />}
    {sheet?.kind === 'details' && <EventSheet title="Details" parts={['details']} done={done} {...sheetProps} />}
    {sheet?.kind === 'time' && <EventSheet title="Time" parts={['time']} done={done} {...sheetProps} />}
    {sheet?.kind === 'contact' && <EventSheet title="Contact" parts={['contact']} done={done} {...sheetProps} />}
    {sheet?.kind === 'hours' && <DaySheet e={e} day={sheet.day} kind="hours" today={today} phone={phone} close={close} done={done} csrf={csrf} />}
    {sheet?.kind === 'remove' && sheet.day && <DaySheet e={e} day={sheet.day} kind="remove" today={today} phone={phone} close={close} done={done} csrf={csrf} />}
    {sheet?.kind === 'remove' && !sheet.day && <RemoveEventSheet e={e} today={today} phone={phone} close={close} csrf={csrf}
      done={(_, toast) => { close(); said(toast); back(true); }} />}
  </Stack>;
}

// --- Every event: the list of S11 (A1, A5).

export default function Events({ config, phone, open, again, setCrumb, eventFocus }) {
  const today = React.useMemo(() => todayIn(config.time_zone), [config.time_zone]);
  const [filters, setFilters] = React.useState({ when: 'coming', from: today, to: addDays(today, 6), category: 'all', asset: 'all', q: '', dir: 'asc', page: 0 });
  const [making, setMaking] = React.useState(false);
  const [chosen, setChosen] = React.useState(config.event ?? null);
  // An event's page has its own address, pushed as the Blocks' booking is, so Back
  // returns to the list and does not leave the admin.
  const openEvent = React.useCallback((id) => {
    if (window.location.pathname !== eventAddress(id)) window.history.pushState({ event: id }, '', eventAddress(id));
    setChosen(id);
  }, []);
  const leave = React.useCallback(() => {
    if (eventOfAddress(window.location.pathname) === null) setChosen(null);
    else if (window.history.state?.event) window.history.back();
    else { window.history.replaceState(null, '', EVENTS_ADDRESS); setChosen(null); }
  }, []);
  React.useEffect(() => {
    const moved = () => setChosen(eventOfAddress(window.location.pathname));
    window.addEventListener('popstate', moved);
    return () => window.removeEventListener('popstate', moved);
  }, []);
  const [said, setSaid] = React.useState('');
  const [changes, setChanges] = React.useState(0);
  React.useEffect(() => { if (eventFocus) openEvent(eventFocus.id); }, [eventFocus, openEvent]);
  const change = (patch) => setFilters((f) => ({ ...f, page: 0, ...patch }));
  const { data, failed, loading } = useRead(readList, { ...eventsQuery(filters), _: changes }, again);
  const { data: one, failed: oneFailed } = useRead(readOne, { id: chosen });
  const [kept, keep] = useKept(one);
  const e = chosen !== null && kept && kept.id === chosen ? kept : null;
  useCrumb(setCrumb, e ? [e.title] : [], leave);
  const toast = <Toast said={said} clear={() => setSaid('')} />;
  const back = (removed = false) => { if (removed) setChanges((n) => n + 1); leave(); };
  if (chosen !== null) {
    return <Stack spacing={2}>
      {oneFailed && <Alert severity="error" variant="outlined" action={<Button color="inherit" size="small" onClick={() => back()}>Events</Button>}>
        {EVENT_WORDS['not-found']}</Alert>}
      {!e && !oneFailed && <Progress loading />}
      {e && <EventPage key={e.id} e={e} today={today} phone={phone} back={back} open={open} said={setSaid} csrf={config.csrf}
        changed={(fresh) => { if (fresh?.id === e.id) keep(fresh); setChanges((n) => n + 1); }} />}
      {toast}
    </Stack>;
  }
  const categories = data?.categories ?? [];
  const assets = data?.assets ?? [];
  const dates = <Dates label="Happening" choices={EVENT_WHEN} when={filters.when} setWhen={(when) => change({ when, dir: startOrder(when) })}
    from={filters.from} setFrom={(from) => change({ from })} to={filters.to} setTo={(to) => change({ to })} phone={phone} />;
  const category = <TextField select size="small" label="Category" value={filters.category} onChange={(ev) => change({ category: ev.target.value })}
    sx={{ minWidth: phone ? 0 : 170, flex: phone ? 1 : 'none' }}>
    <MenuItem value="all">All</MenuItem>
    {categories.map((c) => <MenuItem key={c.key} value={c.key}>{c.label}</MenuItem>)}
  </TextField>;
  const asset = <TextField select size="small" label="Asset" value={filters.asset} onChange={(ev) => change({ asset: ev.target.value })}
    sx={{ minWidth: phone ? 0 : 140, flex: phone ? 1 : 'none' }}>
    <MenuItem value="all">All</MenuItem><MenuItem value="none">None</MenuItem>
    {assets.map((a) => <MenuItem key={a.id} value={a.id}>{a.name}</MenuItem>)}
  </TextField>;
  const search = <TextField size="small" placeholder="Title" value={filters.q} onChange={(ev) => change({ q: ev.target.value })}
    sx={{ flex: 1, maxWidth: phone ? 'none' : 220, minWidth: 140 }}
    slotProps={{ input: { startAdornment: <InputAdornment position="start"><SearchRounded fontSize="small" /></InputAdornment> } }} />;
  const make = <Button variant="contained" startIcon={<AddRounded />} disabled={!data} onClick={() => setMaking(true)} sx={{ flexShrink: 0 }}>{phone ? 'New' : 'New event'}</Button>;
  const pick = (x) => () => openEvent(x.id);
  const rows = data?.events ?? [];
  const ends = (x) => (x.end.slice(0, 10) === x.start.slice(0, 10) ? x.end.slice(11) : `${dayWords(x.end.slice(0, 10), today)} ${x.end.slice(11)}`);
  return <Stack spacing={2}>
    {phone ? <Stack spacing={1.5}>
      <Stack direction="row" spacing={1.5} sx={{ alignItems: 'center' }}><Box sx={{ flex: 1 }}>{dates}</Box>{make}</Stack>
      <Stack direction="row" spacing={1.5}>{category}{asset}</Stack>
      <Box sx={{ display: 'flex' }}>{search}</Box>
    </Stack> : <Stack direction="row" sx={{ flexWrap: 'wrap', gap: 1.5, alignItems: 'center' }}>
      {dates}{category}{asset}{search}<Box sx={{ flex: 1 }} />{make}
    </Stack>}
    {failed && <Alert severity="error" variant="outlined">The events could not be loaded.</Alert>}
    <Progress loading={loading} />
    {data && (phone ? <Stack spacing={1}>
      {rows.map((x) => <OpenCard key={x.id} open={pick(x)}>
        <Stack direction="row" spacing={1} sx={{ alignItems: 'center' }}>
          <Typography variant="body2" sx={{ fontWeight: 600, flex: 1 }}>{x.title}</Typography>
          {x.weekdays && <Chip size="small" variant="outlined" label={daysWords(x.weekdays)} />}
          {x.contact === null && <NoContact />}
        </Stack>
        <Typography variant="body2" sx={{ color: 'text.secondary' }}>{whenWords(x, today)}{x.asset ? ` · ${x.asset.name}` : ''}</Typography>
        <Typography variant="caption" sx={{ color: 'text.secondary' }}>{categoryWord(categories, x.category)}{x.contact ? ` · ${x.contact}` : ''}</Typography>
      </OpenCard>)}
      {!rows.length && <Typography sx={{ color: 'text.secondary', py: 2 }}>No events here.</Typography>}
    </Stack> : <Box sx={boxStyle}>
      <Table size="small" sx={tight}>
        <TableHead><TableRow>
          <TableCell sortDirection={filters.dir}><TableSortLabel active direction={filters.dir}
            onClick={() => change({ dir: filters.dir === 'asc' ? 'desc' : 'asc' })}>Starts</TableSortLabel></TableCell>
          <TableCell>Ends</TableCell><TableCell>Title</TableCell><TableCell>Category</TableCell><TableCell>Asset</TableCell>
          <TableCell>Repeats</TableCell><TableCell>Contact</TableCell>
        </TableRow></TableHead>
        <TableBody>
          {rows.map((x) => <OpenRow key={x.id} open={pick(x)}>
            <TableCell>{dayWords(x.start.slice(0, 10), today)} {x.start.slice(11)}</TableCell>
            <TableCell>{ends(x)}</TableCell>
            <TableCell sx={{ fontWeight: 600 }}>{x.title}</TableCell>
            <TableCell>{categoryWord(categories, x.category)}</TableCell>
            <TableCell>{x.asset ? x.asset.name : '–'}</TableCell>
            <TableCell>{x.weekdays ? daysWords(x.weekdays) : '–'}</TableCell>
            <TableCell>{x.contact ?? <NoContact />}</TableCell>
          </OpenRow>)}
          {!rows.length && <TableRow><TableCell colSpan={7} sx={{ color: 'text.secondary', py: 4, textAlign: 'center' }}>No events here.</TableCell></TableRow>}
        </TableBody>
      </Table>
    </Box>)}
    {data && data.count > data.per_page && <TablePagination component="div" count={data.count} page={filters.page}
      rowsPerPage={data.per_page} rowsPerPageOptions={[data.per_page]} onPageChange={(_, page) => setFilters((f) => ({ ...f, page }))} />}
    {making && <EventSheet title="New event" parts={['details', 'time', 'contact']} options={data} csrf={config.csrf}
      today={today} phone={phone} open={open} close={() => setMaking(false)}
      done={(made, t) => { setMaking(false); setSaid(t); setChanges((n) => n + 1); openEvent(made.made); }} />}
    {toast}
  </Stack>;
}

// An event's days in the way of a closure or the public holidays (A7, S8), each
// with Remove that day in the same step; and the events in a new block's way
// (S4), which it does not block. `days` are the server's rows (day, title,
// repeating, date, start, end).
export function EventsInTheWay({ days, gone = {}, setGone = null, today }) {
  if (!days.length) return null;
  return <Box>
    <Typography variant="body2" sx={{ fontWeight: 600, mb: 0.5 }}>In the way</Typography>
    <Box sx={{ border: 1, borderColor: 'divider', borderRadius: 1.5, maxHeight: 220, overflowY: 'auto' }}>
      {days.map((t, i) => {
        const removed = gone[dayKey(t)];
        return <Stack key={dayKey(t)} direction="row" spacing={1} sx={{ alignItems: 'center', px: 1.5, py: 0.75, borderTop: i ? 1 : 0, borderColor: 'divider' }}>
          <Box sx={{ flex: 1, minWidth: 0, opacity: removed ? 0.6 : 1, textDecoration: removed ? 'line-through' : 'none' }}>
            <Typography variant="body2" sx={{ fontWeight: 600 }}>{t.title}</Typography>
            <Typography variant="caption" sx={{ color: 'text.secondary' }}>{timeWords(t.start, t.end, today)}</Typography>
          </Box>
          {setGone && t.repeating && (removed
            ? <Button size="small" onClick={() => setGone({ ...gone, [dayKey(t)]: false })}>Undo</Button>
            : <Button size="small" color="error" sx={{ flexShrink: 0 }} onClick={() => setGone({ ...gone, [dayKey(t)]: true })}>Remove that day</Button>)}
        </Stack>;
      })}
    </Box>
  </Box>;
}
