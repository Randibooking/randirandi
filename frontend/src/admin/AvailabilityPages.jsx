// SPDX-License-Identifier: AGPL-3.0-only
import React from 'react';
import {
  Alert, Box, Button, Checkbox, Chip, CircularProgress, IconButton, LinearProgress, MenuItem, Stack, Table, TableBody, TableCell,
  TableHead, TableRow, TextField, Typography,
} from '@mui/material';
import AddRounded from '@mui/icons-material/AddRounded';
import ArrowBackRounded from '@mui/icons-material/ArrowBackRounded';
import ChevronLeftRounded from '@mui/icons-material/ChevronLeftRounded';
import ChevronRightRounded from '@mui/icons-material/ChevronRightRounded';
import CloseRounded from '@mui/icons-material/CloseRounded';
import { Sheet } from '../book/AddSheet';
import { moneyWords } from '../book/add.js';
import { gray } from '../theme/themePrimitives';
import { applyAvailability, readAvailability, readAvailabilityMonth, readEventDaysInTheWay, readHolidays } from './api';
import { EventsInTheWay } from './EventPages';
import { clearOf, holidaysInTheWayQuery, inTheWayQuery, withRemoved } from './events';
import useRead from './useRead';

// No events to look for: an answer of nothing.
const noRead = async () => ({ ok: true, data: null });
import {
  DAY_MINUTES, DAY_NAMES, QUARTERS, STATE_WORDS, WEEKDAYS, addDays, availabilityFailure, barOf, clock, closureBody, closureChanged,
  closureOk, closureRange, confirmLabel, copyWeek, dayOfMonth, dayWords, editable, fixedHoliday, gridOf, holidayLabel, holidaysBody,
  inSupportedYears, longDay, membersLine, monthKey, monthTitle, moreThanListed, noChange, reasonWords, refundLines, removeClosureBody,
  removeWeekBody, savedWords, shiftMonth, tickedHolidays, owedWords, byHandWords, weekBody, weekChanged, weekOk, weekRuns, weekToCopy,
  cancelsTitle, spansText,
} from './availability';
import { Card, Field } from './parts';
import { Problems, Toast } from './setupParts';

// An asset's availability on a page of its own. The month as it falls, the weekly availabilities by start
// date, the closures by date and the public holidays. A change that would cancel
// bookings first asks: it lists them, the members and the refund in each
// currency, then cancels and refunds them (god mode, W10 and W11). The server
// (/admin/assets/<id>/availability/) keeps every rule.

const FREE = gray[50];
const tight = { '& td, & th': { whiteSpace: 'nowrap', px: 1.5 } };
const dateField = { colorScheme: 'dark' };
const money = (amount, currency) => moneyWords(amount, currency);

// --- What a change asks before it is saved.

// One change at a time: ask the server what it would cancel (a preview, nothing
// kept), then save it with the answer's token. A 409 "changed" brings the fresh
// preview, which is asked again.
function useChange({ csrf, asset, onSaved }) {
  const [state, setState] = React.useState({ phase: 'edit', preview: null, busy: false, problem: null });
  const set = (next) => setState((was) => ({ ...was, ...next }));
  const post = async (body) => {
    const result = await applyAvailability(csrf, asset, body);
    if (result.kind === 'signed-out') {
      window.location.assign('/login/');
      return null;
    }
    return result;
  };
  // Looks first; saves at once when nothing would be cancelled.
  const ask = async (body) => {
    if (state.busy) return;
    set({ busy: true, problem: null });
    const result = await post({ ...body, preview: true });
    if (result === null) return;
    if (!result.ok) {
      set({ busy: false, problem: availabilityFailure(result) });
      return;
    }
    const { preview } = result.data;
    if (noChange(preview)) await save(body, preview, true);
    else set({ busy: false, phase: 'confirm', preview });
  };
  const save = async (body, preview, direct = false) => {
    set({ busy: true, problem: null });
    const result = await post({ ...body, expect: preview.token });
    if (result === null) return;
    if (result.ok) {
      onSaved(result.data);
      return;
    }
    if (result.code === 'changed' && result.body?.preview) {
      // The bookings are not those that were shown: look again.
      set({ busy: false, phase: 'confirm', preview: result.body.preview, problem: availabilityFailure(result) });
      return;
    }
    set({ busy: false, phase: direct ? 'edit' : state.phase, problem: availabilityFailure(result) });
  };
  return {
    ...state,
    ask,
    save,
    back: () => set({ phase: 'edit', problem: null }),
    clear: () => set({ problem: null }),
  };
}

// The bookings a change would cancel, the members and what is refunded.
function Cancels({ preview }) {
  const more = moreThanListed(preview);
  return <Stack spacing={1.5}>
    <Typography variant="subtitle2">{cancelsTitle(preview)}</Typography>
    <Box sx={{ border: 1, borderColor: 'divider', borderRadius: 1.5, maxHeight: 220, overflowY: 'auto' }}>
      {preview.listed.map((item, i) => <Box key={item.id} sx={{ px: 1.5, py: 1, borderTop: i ? 1 : 0, borderColor: 'divider' }}>
        <Stack direction="row" spacing={1}>
          <Typography variant="body2" sx={{ fontWeight: 600 }}>Booking {item.reference}</Typography>
          <Box sx={{ flex: 1 }} />
          <Typography variant="body2">{item.amount ? money(item.amount, item.currency) : '–'}</Typography>
        </Stack>
        <Typography variant="caption" sx={{ color: 'text.secondary' }}>{item.start} to {item.end} · {item.member}</Typography>
      </Box>)}
      {more > 0 && <Typography variant="caption" sx={{ display: 'block', px: 1.5, py: 1, color: 'text.secondary', borderTop: 1, borderColor: 'divider' }}>
        + {more} more</Typography>}
    </Box>
    <Field label="Members">{membersLine(preview)}</Field>
    <Field label="Refund">{preview.refund.length ? refundLines(preview, money).join(', ') : '–'}</Field>
  </Stack>;
}

// The question before a change that cancels: Back to the sheet, or Save and cancel.
function Confirm({ title, kind, change, phone, onClose, body }) {
  const { preview, busy, problem } = change;
  return <Sheet wide={!phone} title={<Typography variant="h6" sx={{ fontWeight: 600 }}>{title}</Typography>}
    onClose={busy ? () => {} : onClose}>
    <Stack spacing={2}>
      <Cancels preview={preview} />
      <Problems problem={problem} />
      <Stack direction="row" spacing={1} sx={{ justifyContent: 'flex-end' }}>
        <Button onClick={change.back} disabled={busy}>Back</Button>
        <Button variant="contained" color="error" disabled={busy} onClick={() => change.save(body, preview)}
          startIcon={busy ? <CircularProgress size={16} color="inherit" /> : null}>{confirmLabel(kind, preview)}</Button>
      </Stack>
    </Stack>
  </Sheet>;
}

function Buttons({ close, ok, busy, label = 'Save', left = null, submit }) {
  return <Stack direction="row" spacing={1} sx={{ pt: 0.5 }}>
    {left}<Box sx={{ flex: 1 }} />
    <Button onClick={close} disabled={busy}>Cancel</Button>
    <Button type="submit" variant="contained" disabled={!ok || busy} onClick={submit}
      startIcon={busy ? <CircularProgress size={16} color="inherit" /> : null}>{label}</Button>
  </Stack>;
}

// --- The sheets.

// A new weekly availability from a date, or one still to come changed.
function WeekSheet({ row, weeks, today, csrf, asset, phone, onClose, onSaved }) {
  const start = row?.from ?? addDays(today, 1);
  const [from, setFrom] = React.useState(start);
  const [week, setWeek] = React.useState(() => copyWeek(row?.week ?? weekToCopy(weeks, start)));
  const change = useChange({ csrf, asset, onSaved });
  const body = weekBody(row, { from, week });
  const ok = weekOk(week) && from >= today && weekChanged(row, week, from);
  const title = row ? `Weekly availability from ${row.from}` : 'New weekly availability';
  const set = (d, i, k, v) => setWeek(week.map((list, j) => (j !== d ? list : list.map((x, n) => (n === i ? (k === 0 ? [v, x[1]] : [x[0], v]) : x)))));
  const time = (d, i, k) => <TextField select size="small" value={week[d][i][k]} disabled={change.busy}
    onChange={(e) => { set(d, i, k, e.target.value); change.clear(); }}
    slotProps={{ select: { MenuProps: { slotProps: { paper: { sx: { maxHeight: 280 } } } } }, htmlInput: { 'aria-label': `${DAY_NAMES[d]} ${k === 0 ? 'from' : 'to'}` } }}
    sx={{ width: 104 }}>
    {QUARTERS.filter((m) => (k === 0 ? m < DAY_MINUTES : m > 0)).map((m) => <MenuItem key={m} value={m}>{clock(m)}</MenuItem>)}
  </TextField>;
  if (change.phase === 'confirm') {
    return <Confirm title={title} kind="save" change={change} phone={phone} onClose={onClose} body={body} />;
  }
  return <Sheet wide={!phone} title={<Typography variant="h6" sx={{ fontWeight: 600 }}>{title}</Typography>}
    onClose={change.busy ? () => {} : onClose}>
    <Stack spacing={1.5} component="form" onSubmit={(e) => { e.preventDefault(); if (ok) change.ask(body); }}>
      <TextField type="date" size="small" label="From" value={from} disabled={change.busy}
        onChange={(e) => { if (e.target.value) { setFrom(e.target.value); change.clear(); } }}
        slotProps={{ htmlInput: { min: today }, inputLabel: { shrink: true } }} sx={{ width: 170, ...dateField }} />
      {DAY_NAMES.map((name, d) => <Box key={name}>
        <Stack direction="row" sx={{ alignItems: 'center' }}>
          <Typography variant="body2" sx={{ fontWeight: 600, flex: 1 }}>{name}</Typography>
          <IconButton size="small" aria-label={`Add time on ${name}`} disabled={change.busy}
            onClick={() => setWeek(week.map((list, j) => (j === d ? [...list, [600, 1320]] : list)))}>
            <AddRounded fontSize="small" /></IconButton>
        </Stack>
        {!week[d].length && <Typography variant="body2" sx={{ color: 'text.secondary' }}>Closed</Typography>}
        {week[d].map((x, i) => <Stack key={i} direction="row" spacing={1} sx={{ alignItems: 'center', mt: 0.75 }}>
          {time(d, i, 0)}<Typography variant="body2">–</Typography>{time(d, i, 1)}
          <IconButton size="small" aria-label="Remove" disabled={change.busy}
            onClick={() => setWeek(week.map((list, j) => (j === d ? list.filter((_, n) => n !== i) : list)))}>
            <CloseRounded fontSize="small" /></IconButton>
        </Stack>)}
      </Box>)}
      <Problems problem={change.problem} />
      <Buttons close={onClose} ok={ok} busy={change.busy} submit={() => ok && change.ask(body)} />
    </Stack>
  </Sheet>;
}

// A weekly availability still to come removed: the one before it applies on.
function RemoveWeek({ row, csrf, asset, phone, onClose, onSaved }) {
  const change = useChange({ csrf, asset, onSaved });
  const body = removeWeekBody(row);
  const title = `Remove weekly availability from ${row.from}?`;
  if (change.phase === 'confirm') {
    return <Confirm title={title} kind="remove" change={change} phone={phone} onClose={onClose} body={body} />;
  }
  return <Sheet wide={!phone} title={<Typography variant="h6" sx={{ fontWeight: 600 }}>{title}</Typography>}
    onClose={change.busy ? () => {} : onClose}>
    <Stack spacing={2}>
      <Problems problem={change.problem} />
      <Stack direction="row" spacing={1} sx={{ justifyContent: 'flex-end' }}>
        <Button onClick={onClose} disabled={change.busy}>Cancel</Button>
        <Button variant="contained" color="error" disabled={change.busy} onClick={() => change.ask(body)}
          startIcon={change.busy ? <CircularProgress size={16} color="inherit" /> : null}>Remove</Button>
      </Stack>
    </Stack>
  </Sheet>;
}

// A closure still to come removed: asked first, as a weekly availability is.
function RemoveClosure({ row, csrf, asset, phone, onClose, onSaved }) {
  const change = useChange({ csrf, asset, onSaved });
  const body = removeClosureBody(row);
  const title = `Remove closure ${closureRange(row)}?`;
  if (change.phase === 'confirm') {
    return <Confirm title={title} kind="remove" change={change} phone={phone} onClose={onClose} body={body} />;
  }
  return <Sheet wide={!phone} title={<Typography variant="h6" sx={{ fontWeight: 600 }}>{title}</Typography>}
    onClose={change.busy ? () => {} : onClose}>
    <Stack spacing={2}>
      <Problems problem={change.problem} />
      <Stack direction="row" spacing={1} sx={{ justifyContent: 'flex-end' }}>
        <Button onClick={onClose} disabled={change.busy}>Cancel</Button>
        <Button variant="contained" color="error" disabled={change.busy} onClick={() => change.ask(body)}
          startIcon={change.busy ? <CircularProgress size={16} color="inherit" /> : null}>Remove</Button>
      </Stack>
    </Stack>
  </Sheet>;
}

// A closure by date: new, or one that is not over changed or removed.
function ClosureSheet({ row, reasons, today, csrf, asset, phone, onClose, onSaved }) {
  const [first, setFirst] = React.useState(row?.first ?? addDays(today, 14));
  const [last, setLast] = React.useState(row?.last ?? addDays(today, 14));
  const [reason, setReason] = React.useState(row?.reason ?? '');
  const [removing, setRemoving] = React.useState(false);
  const change = useChange({ csrf, asset, onSaved });
  const draft = { first, last, reason };
  // The events' days the closure would cover: a repeat's can be removed in the same save.
  const [gone, setGone] = React.useState({});
  const looking = closureOk(first, last, today);
  const way = useRead(looking ? readEventDaysInTheWay : noRead, inTheWayQuery(asset, first, last));
  const days = looking ? (way.data?.days ?? []) : [];
  const saving = withRemoved(closureBody(row, draft), days, gone);
  const body = removing ? removeClosureBody(row) : saving;
  const ok = looking && closureChanged(row, draft) && !(looking && way.loading) && !way.failed && clearOf(days, gone);
  const title = row ? `Closure ${closureRange(row)}` : 'New closure';
  if (change.phase === 'confirm') {
    return <Confirm title={title} kind={removing ? 'remove' : 'save'} change={change} phone={phone}
      onClose={onClose} body={body} />;
  }
  const field = (label, value, set, min) => <TextField type="date" size="small" label={label} value={value} disabled={change.busy}
    onChange={(e) => {
      if (!e.target.value) return;
      set(e.target.value);
      change.clear();
    }}
    slotProps={{ htmlInput: { min }, inputLabel: { shrink: true } }} sx={dateField} />;
  const remove = row && <Button color="error" disabled={change.busy} onClick={() => { setRemoving(true); change.ask(removeClosureBody(row)); }}>
    Remove</Button>;
  return <Sheet wide={!phone} title={<Typography variant="h6" sx={{ fontWeight: 600 }}>{title}</Typography>}
    onClose={change.busy ? () => {} : onClose}>
    <Stack spacing={2} component="form" onSubmit={(e) => { e.preventDefault(); if (ok) change.ask(body); }}>
      {field('First date', first, (v) => { setFirst(v); if (last < v) setLast(v); }, row ? undefined : today)}
      {field('Last date', last, setLast, first)}
      <TextField select size="small" label="Reason" value={reason} disabled={change.busy} onChange={(e) => setReason(e.target.value)}>
        <MenuItem value="">None</MenuItem>
        {reasons.map((r) => <MenuItem key={r.value} value={r.value}>{r.label}</MenuItem>)}
      </TextField>
      <EventsInTheWay days={days} gone={gone} setGone={setGone} today={today} />
      <Problems problem={change.problem} />
      <Buttons close={onClose} ok={ok} busy={change.busy} left={remove} submit={() => { setRemoving(false); if (ok) change.ask(saving); }} />
    </Stack>
  </Sheet>;
}

// A year's public holidays added as closures, each a whole day; one on a Sunday
// is left out unless ticked, one past or closed already cannot be.
function HolidaySheet({ asset, today, csrf, phone, onClose, onSaved }) {
  const [year, setYear] = React.useState(Number(today.slice(0, 4)));
  const [read, setRead] = React.useState(null);
  const [failed, setFailed] = React.useState(false);
  const [picked, setPicked] = React.useState(new Set());
  const change = useChange({ csrf, asset, onSaved });
  React.useEffect(() => {
    let current = true;
    setFailed(false);
    readHolidays(asset, year).then((result) => {
      if (!current) return;
      if (result.kind === 'signed-out') window.location.assign('/login/');
      else if (result.ok) {
        setRead(result.data);
        setPicked(tickedHolidays(result.data.holidays));
      } else setFailed(true);
    });
    return () => { current = false; };
  }, [asset, year]);
  const holidays = read?.holidays ?? [];
  const picks = holidaysBody(year, picked, holidays);
  const count = picks.dates.length;
  const [gone, setGone] = React.useState({});
  const looking = count > 0 && count <= 20;
  const way = useRead(looking ? readEventDaysInTheWay : noRead, holidaysInTheWayQuery(asset, picks.dates));
  const days = looking ? (way.data?.days ?? []) : [];
  const body = withRemoved(picks, days, gone);
  const ok = looking && !way.loading && !way.failed && clearOf(days, gone);
  if (change.phase === 'confirm') {
    return <Confirm title="Public holidays" kind="save" change={change} phone={phone} onClose={onClose} body={body} />;
  }
  return <Sheet wide={!phone} title={<Typography variant="h6" sx={{ fontWeight: 600 }}>Public holidays</Typography>}
    onClose={change.busy ? () => {} : onClose}>
    <Stack spacing={1.5}>
      <TextField select size="small" label="Year" value={year} disabled={change.busy} onChange={(e) => setYear(e.target.value)} sx={{ width: 120 }}>
        {(read?.years ?? [year]).map((v) => <MenuItem key={v} value={v}>{v}</MenuItem>)}
      </TextField>
      {!read && !failed && <LinearProgress />}
      {failed && <Alert severity="error" variant="outlined">The holidays could not be loaded.</Alert>}
      <Box>
        {holidays.map((h) => <Stack key={h.date} direction="row" spacing={1} sx={{ alignItems: 'center', opacity: fixedHoliday(h) ? 0.5 : 1 }}>
          <Checkbox size="small" checked={fixedHoliday(h) ? h.closed : picked.has(h.date)} disabled={fixedHoliday(h) || change.busy}
            slotProps={{ input: { 'aria-label': `${h.date} ${h.name}` } }}
            onChange={() => setPicked((was) => { const n = new Set(was); if (n.has(h.date)) n.delete(h.date); else n.add(h.date); return n; })} />
          <Typography variant="body2" sx={{ width: 92, flexShrink: 0 }}>{h.date}</Typography>
          <Typography variant="body2" sx={{ width: 34, flexShrink: 0, color: 'text.secondary' }}>{WEEKDAYS[h.weekday]}</Typography>
          <Typography variant="body2" sx={{ flex: 1 }}>{h.name}</Typography>
          {h.closed && <Typography variant="caption" sx={{ color: 'text.secondary' }}>Added</Typography>}
        </Stack>)}
      </Box>
      <EventsInTheWay days={days} gone={gone} setGone={setGone} today={today} />
      <Problems problem={change.problem} />
      <Stack direction="row" spacing={1} sx={{ justifyContent: 'flex-end' }}>
        <Button onClick={onClose} disabled={change.busy}>Cancel</Button>
        <Button variant="contained" disabled={!ok || change.busy} onClick={() => change.ask(body)}
          startIcon={change.busy ? <CircularProgress size={16} color="inherit" /> : null}>{holidayLabel(count)}</Button>
      </Stack>
    </Stack>
  </Sheet>;
}

// --- The month as it falls: each day's open time after its weekly availability
// and closures, as a bar of the day and, on a wide screen, in words.

function Bar({ day }) {
  const { open, cut } = barOf(day);
  const piece = ([s, e], color) => <Box key={`${color}${s}`} sx={{ position: 'absolute', top: 0, bottom: 0, bgcolor: color,
    left: `${(s / DAY_MINUTES) * 100}%`, width: `${((e - s) / DAY_MINUTES) * 100}%` }} />;
  return <Box sx={{ position: 'relative', height: 6, borderRadius: 1, overflow: 'hidden', border: 1, borderColor: 'divider', my: 0.5 }}>
    {open.map((x) => piece(x, FREE))}{cut.map((x) => piece(x, 'error.main'))}
  </Box>;
}

function Month({ asset, today, again, page, phone }) {
  const [key, setKey] = React.useState(monthKey(today));
  const [picked, setPicked] = React.useState(today);
  const [days, setDays] = React.useState(null);
  const [failed, setFailed] = React.useState(false);
  React.useEffect(() => {
    let current = true;
    setFailed(false);
    readAvailabilityMonth(asset, key).then((result) => {
      if (!current) return;
      if (result.kind === 'signed-out') window.location.assign('/login/');
      else if (result.ok) setDays(result.data.days);
      else setFailed(true);
    });
    return () => { current = false; };
  }, [asset, key, again]);
  const step = (n) => setKey(shiftMonth(key, n));
  const cells = days ? gridOf(days) : [];
  const day = days?.find((d) => d.date === picked) ?? null;
  const weekOfDay = day && page.weeks.filter((w) => w.from <= picked).sort((a, b) => (a.from < b.from ? 1 : -1))[0];
  const closures = page.closures.filter((c) => c.first <= picked && c.last >= picked);
  return <Card title={monthTitle(key)} action={<Stack direction="row">
    <IconButton size="small" aria-label="Previous month" disabled={!inSupportedYears(shiftMonth(key, -1))} onClick={() => step(-1)}>
      <ChevronLeftRounded fontSize="small" /></IconButton>
    <IconButton size="small" aria-label="Next month" disabled={!inSupportedYears(shiftMonth(key, 1))} onClick={() => step(1)}>
      <ChevronRightRounded fontSize="small" /></IconButton>
  </Stack>}>
    {failed && <Alert severity="error" variant="outlined">The month could not be loaded.</Alert>}
    {!days && !failed && <LinearProgress />}
    {days && <Box sx={{ display: 'grid', gridTemplateColumns: 'repeat(7, minmax(0, 1fr))', gap: 0.5 }}>
      {WEEKDAYS.map((d) => <Typography key={d} variant="caption" sx={{ color: 'text.secondary', px: 0.5 }}>{d}</Typography>)}
      {cells.map((cell, i) => {
        if (cell === null) return <Box key={`x${i}`} />;
        const words = dayWords(cell);
        const today_ = cell.date === today;
        return <Box key={cell.date} role="button" tabIndex={0} aria-label={`${cell.date}`} onClick={() => setPicked(cell.date)}
          onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); setPicked(cell.date); } }}
          sx={{ minHeight: phone ? 44 : 76, p: 0.75, borderRadius: 1, cursor: 'pointer', border: 1, minWidth: 0,
            borderColor: cell.date === picked ? 'primary.main' : 'divider', opacity: cell.date < today ? 0.5 : 1 }}>
          <Stack direction="row" spacing={0.5} sx={{ alignItems: 'center' }}>
            <Typography variant="caption" sx={{ fontWeight: today_ ? 700 : 500, color: today_ ? 'primary.light' : 'text.primary' }}>
              {dayOfMonth(cell.date)}</Typography>
            {cell.week_starts && <Box aria-label="New weekly availability" sx={{ width: 6, height: 6, borderRadius: '50%', bgcolor: 'primary.main' }} />}
          </Stack>
          <Bar day={cell} />
          {!phone && <Typography variant="caption" component="div" sx={{ color: cell.closed ? 'error.light' : 'text.secondary', lineHeight: 1.35 }}>
            {words ?? cell.open.map(([s, e]) => <div key={s}>{clock(s)}–{clock(e)}</div>)}</Typography>}
        </Box>;
      })}
    </Box>}
    <Stack direction="row" spacing={2} sx={{ mt: 1.5, mb: 1, flexWrap: 'wrap' }}>
      {[[FREE, 'Open'], ['error.main', 'Closure'], ['primary.main', 'New weekly availability']].map(([color, text]) => <Stack key={text} direction="row" spacing={0.75} sx={{ alignItems: 'center' }}>
        <Box sx={{ width: 10, height: 10, borderRadius: text === 'New weekly availability' ? '50%' : 0.5, bgcolor: color }} />
        <Typography variant="caption" sx={{ color: 'text.secondary' }}>{text}</Typography>
      </Stack>)}
    </Stack>
    {day && <Box sx={{ borderTop: 1, borderColor: 'divider', pt: 1.25 }}>
      <Typography variant="body2" sx={{ fontWeight: 600, mb: 0.5 }}>{longDay(picked)}</Typography>
      <Field label="Open">{day.closed || !day.open.length ? 'Closed' : spansText(day.open)}</Field>
      <Field label="Weekly">{weekOfDay ? `From ${weekOfDay.from}` : '–'}</Field>
      {closures.map((c) => <Field key={c.id} label="Closure">{closureRange(c)}{c.reason ? ` · ${reasonWords(c.reason)}` : ''}</Field>)}
    </Box>}
  </Card>;
}

// --- The page.

function WeekList({ page, phone, edit, remove, make }) {
  const weeks = page.weeks;
  const chip = (w) => <Chip size="small" variant="outlined" color={STATE_WORDS[w.state][1]} label={STATE_WORDS[w.state][0]} />;
  const actions = (w) => (editable(w) ? <Stack direction="row" spacing={0.5}>
    <Button size="small" onClick={() => edit(w)}>Edit</Button>
    <Button size="small" color="error" onClick={() => remove(w)}>Remove</Button>
  </Stack> : null);
  return <Card flush title={`Weekly availability (${weeks.length})`}
    action={<Button size="small" startIcon={<AddRounded />} onClick={make}>New</Button>}>
    {!weeks.length && <Typography variant="body2" sx={{ color: 'text.secondary', px: 2, pb: 2 }}>None</Typography>}
    {weeks.length > 0 && (phone ? weeks.map((w) => <Box key={w.id} sx={{ px: 2, py: 1.25, borderTop: 1, borderColor: 'divider' }}>
      <Stack direction="row" spacing={1} sx={{ alignItems: 'center' }}>
        <Typography variant="body2" sx={{ fontWeight: 600, flex: 1 }}>From {w.from}</Typography>{chip(w)}
      </Stack>
      {weekRuns(w.week).map((r) => <Typography key={r} variant="body2" sx={{ color: 'text.secondary' }}>{r}</Typography>)}
      <Typography variant="caption" sx={{ color: 'text.secondary' }}>{w.by ? `${w.by} · ${w.at}` : '–'}</Typography>
      {actions(w)}
    </Box>) : <Table size="small" sx={tight}>
      <TableHead><TableRow>
        <TableCell>From</TableCell><TableCell>Until</TableCell><TableCell>Week</TableCell><TableCell>State</TableCell>
        <TableCell>Made by</TableCell><TableCell padding="checkbox" />
      </TableRow></TableHead>
      <TableBody>
        {weeks.map((w) => <TableRow key={w.id}>
          <TableCell sx={{ fontWeight: 600 }}>{w.from}</TableCell><TableCell>{w.until ?? '–'}</TableCell>
          <TableCell>{weekRuns(w.week).map((r) => <div key={r}>{r}</div>)}</TableCell>
          <TableCell>{chip(w)}</TableCell><TableCell>{w.by || '–'}</TableCell>
          <TableCell align="right">{actions(w)}</TableCell>
        </TableRow>)}
      </TableBody>
    </Table>)}
  </Card>;
}

function ClosureList({ page, phone, edit, remove, holidays, make }) {
  const list = page.closures;
  const actions = (c) => (c.over ? null : <Stack direction="row" spacing={0.5}>
    <Button size="small" onClick={() => edit(c)}>Edit</Button>
    <Button size="small" color="error" onClick={() => remove(c)}>Remove</Button>
  </Stack>);
  return <Card flush title={`Closures (${list.length})`} action={<Stack direction="row" spacing={0.5}>
    <Button size="small" onClick={holidays}>Public holidays</Button>
    <Button size="small" startIcon={<AddRounded />} onClick={make}>New</Button>
  </Stack>}>
    {!list.length ? <Typography variant="body2" sx={{ color: 'text.secondary', px: 2, pb: 2 }}>None</Typography>
      : phone ? list.map((c) => <Stack key={c.id} direction="row" sx={{ px: 2, py: 1.1, borderTop: 1, borderColor: 'divider', alignItems: 'center' }}>
        <Box sx={{ flex: 1 }}>
          <Typography variant="body2" sx={{ fontWeight: 600 }}>{c.first}</Typography>
          {c.last !== c.first && <Typography variant="body2" sx={{ color: 'text.secondary' }}>to {c.last}</Typography>}
          <Typography variant="caption" sx={{ color: 'text.secondary' }}>{reasonWords(c.reason)}{c.by ? ` · ${c.by}` : ''}</Typography>
        </Box>
        {actions(c)}
      </Stack>) : <Table size="small" sx={tight}>
        <TableHead><TableRow>
          <TableCell>First date</TableCell><TableCell>Last date</TableCell><TableCell>Reason</TableCell>
          <TableCell>Made by</TableCell><TableCell>Created</TableCell><TableCell padding="checkbox" />
        </TableRow></TableHead>
        <TableBody>
          {list.map((c) => <TableRow key={c.id}>
            <TableCell>{c.first}</TableCell><TableCell>{c.last}</TableCell><TableCell>{reasonWords(c.reason)}</TableCell>
            <TableCell>{c.by || '–'}</TableCell><TableCell sx={{ color: 'text.secondary' }}>{c.at || '–'}</TableCell>
            <TableCell align="right">{actions(c)}</TableCell>
          </TableRow>)}
        </TableBody>
      </Table>}
  </Card>;
}

// What a save left to do by hand: an order cancelled whose money could not be
// recorded as owed.
function ByHand({ rows }) {
  return <Alert severity="warning" variant="outlined">
    <Typography variant="body2" sx={{ fontWeight: 600 }}>Refunds by hand ({rows.length})</Typography>
    {rows.map((row) => <Typography key={row.number} variant="body2">{byHandWords(row)}</Typography>)}
  </Alert>;
}

// The refunds queued and not yet taken by the payment provider; the server sends
// them again until it does.
function Owed({ rows }) {
  return <Alert severity="info" variant="outlined">
    <Typography variant="body2" sx={{ fontWeight: 600 }}>Refunds owed ({rows.length})</Typography>
    {rows.map((row) => <Typography key={row.id} variant="body2">{owedWords(row, money)}</Typography>)}
  </Alert>;
}

export default function AvailabilityPage({ config, phone, asset, back, onChanged }) {
  const [page, setPage] = React.useState(null);
  const [failed, setFailed] = React.useState(false);
  const [sheet, setSheet] = React.useState(null);
  const [said, setSaid] = React.useState('');
  const [again, setAgain] = React.useState(0);
  React.useEffect(() => {
    let current = true;
    readAvailability(asset.id).then((result) => {
      if (!current) return;
      if (result.kind === 'signed-out') window.location.assign('/login/');
      else if (result.ok) { setPage(result.data); setFailed(false); } else setFailed(true);
    });
    return () => { current = false; };
  }, [asset.id]);
  const close = () => setSheet(null);
  // A save answers the page as it is; the month is read again and the asset's
  // place learns of it when the page is left.
  const onSaved = (answer) => {
    setPage(answer.page);
    setSheet(null);
    setSaid(savedWords(answer));
    setAgain((n) => n + 1);
    onChanged();
  };
  const props = { csrf: config.csrf, asset: asset.id, phone, onClose: close, onSaved };
  return <Stack spacing={2}>
    {phone && <Button startIcon={<ArrowBackRounded />} onClick={back} sx={{ alignSelf: 'flex-start' }}>{asset.name}</Button>}
    <Typography component="h1" variant="h5" sx={{ fontWeight: 600 }}>Availability</Typography>
    {!page && !failed && <LinearProgress />}
    {failed && !page && <Alert severity="error">The availability could not be loaded.</Alert>}
    {page && <>
      {(page.owed ?? []).length > 0 && <Owed rows={page.owed} />}
      {(page.by_hand ?? []).length > 0 && <ByHand rows={page.by_hand} />}
      <Month asset={asset.id} today={page.today} again={again} page={page} phone={phone} />
      <WeekList page={page} phone={phone} make={() => setSheet({ kind: 'week', row: null })}
        edit={(row) => setSheet({ kind: 'week', row })} remove={(row) => setSheet({ kind: 'remove', row })} />
      <ClosureList page={page} phone={phone} make={() => setSheet({ kind: 'closure', row: null })}
        edit={(row) => setSheet({ kind: 'closure', row })} remove={(row) => setSheet({ kind: 'remove-closure', row })} holidays={() => setSheet({ kind: 'holidays' })} />
      {sheet?.kind === 'week' && <WeekSheet key={sheet.row?.id ?? 'new'} row={sheet.row} weeks={page.weeks} today={page.today} {...props} />}
      {sheet?.kind === 'remove' && <RemoveWeek row={sheet.row} {...props} />}
      {sheet?.kind === 'closure' && <ClosureSheet key={sheet.row?.id ?? 'new'} row={sheet.row} reasons={page.reasons} today={page.today} {...props} />}
      {sheet?.kind === 'remove-closure' && <RemoveClosure row={sheet.row} {...props} />}
      {sheet?.kind === 'holidays' && <HolidaySheet today={page.today} {...props} />}
    </>}
    <Toast said={said} clear={() => setSaid('')} />
  </Stack>;
}
