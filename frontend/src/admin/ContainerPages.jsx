// SPDX-License-Identifier: AGPL-3.0-only
import React from 'react';
import {
  Alert, Box, Button, Checkbox, Chip, IconButton, LinearProgress, Link, MenuItem, Stack, Switch, Table, TableBody, TableCell,
  TableHead, TableRow, TextField, Typography,
} from '@mui/material';
import AddRounded from '@mui/icons-material/AddRounded';
import ArrowBackRounded from '@mui/icons-material/ArrowBackRounded';
import CloseRounded from '@mui/icons-material/CloseRounded';
import LockRounded from '@mui/icons-material/LockRounded';
import { Sheet } from '../book/AddSheet';
import {
  changeContainer, changeContainerCategories, makeContainer, readContainers, removeContainer,
} from './api';
import { counted } from './assets';
import { useCrumb } from './crumbs';
import { Card, Field, OpenCard, OpenRow, boxStyle } from './parts';
import {
  FLEXIBLE_LENGTHS, QUARTERS, WEEKDAYS, categoriesBody, categoriesChanged, categoriesDraft, categoryNames, changed,
  containerFailure, containerNo, daysText, detailsBody, detailsDraft, endDayChoices, everyChoices, freshHours, hm,
  intervalChoices, length, lengthChoices, newBody, newDraft, newReady, nameOf, noWeeks, ownText, paymentText, periodLength,
  periodTypeWords, tickCategory, timeBody, timeDraft, timesText, usesText, weekChoices, weekText, weeksText, DAY,
} from './containers';
import { Problems, SheetButtons, Toast, useKept } from './setupParts';
import useRead from './useRead';

// The Containers: every container by asset; a container's page with its details, its time,
// its calendar weeks and its booking categories with their own hours ();
// one sheet for each card. The server (/admin/containers/...) keeps the rules:
// a Save the validator refuses shows the model's own words, and the place is
// answered again after every change. While a booking is still to come the
// fields it depends on show a lock (docs/booking-model.md, "After a container is
// saved"). The prices on a container belong to the price books (7.97): the
// categories show only whether the container has a price.

const tight = { '& td, & th': { whiteSpace: 'nowrap', px: 1.25 } };
const wrap = { whiteSpace: 'normal !important', minWidth: 130 };

const Locked = ({ on }) => (on
  ? <LockRounded aria-label="Locked" sx={{ fontSize: 15, color: 'text.secondary', ml: 0.75, verticalAlign: '-2px' }} />
  : null);
const StatusChip = ({ c }) => <Chip size="small" variant="outlined" color={c.status === 'enabled' ? 'success' : 'default'}
  label={c.status === 'enabled' ? 'Enabled' : 'Disabled'} />;

// One call at a time: the answer closes the sheet, a refusal stays on it.
function useRun(done) {
  const [busy, setBusy] = React.useState(false);
  const [problems, setProblems] = React.useState([]);
  const running = React.useRef(false);
  const run = async (call, toast) => {
    if (running.current) return;
    running.current = true;
    setBusy(true);
    setProblems([]);
    const result = await call();
    if (result.kind === 'signed-out') {
      window.location.assign('/login/');
      return;
    }
    running.current = false;
    setBusy(false);
    if (result.ok) done(result.data, toast);
    else setProblems(containerFailure(result));
  };
  return { busy, problems, run, clear: () => setProblems([]) };
}

function Choice({ label, value, set, options, disabled, width }) {
  return <TextField select size="small" label={label} value={value} disabled={disabled} onChange={(e) => set(e.target.value)}
    sx={{ width: width ?? '100%' }}>
    {options.map(([v, text]) => <MenuItem key={String(v)} value={v}>{text}</MenuItem>)}
  </TextField>;
}

function TimeSelect({ label, value, set, from = 0, to = DAY, disabled }) {
  const options = [...new Set([...QUARTERS.filter((m) => m >= from && m <= to), value])].sort((a, b) => a - b);
  return <TextField select size="small" label={label} value={value} disabled={disabled} onChange={(e) => set(e.target.value)}
    slotProps={{ select: { MenuProps: { slotProps: { paper: { sx: { maxHeight: 280 } } } } } }} sx={{ width: 104 }}>
    {options.map((m) => <MenuItem key={m} value={m}>{hm(m)}</MenuItem>)}
  </TextField>;
}

function Days({ value, set, disabled }) {
  return <Stack direction="row" sx={{ flexWrap: 'wrap', gap: 0.5 }}>
    {WEEKDAYS.map((d, i) => <Chip key={d} label={d} size="small" disabled={disabled} color={value.includes(i) ? 'primary' : 'default'}
      variant={value.includes(i) ? 'filled' : 'outlined'}
      onClick={() => set(value.includes(i) ? value.filter((x) => x !== i) : [...value, i].sort((a, b) => a - b))} />)}
  </Stack>;
}

// A calendar week from this week on, two years ahead; none is "–".
function WeekSelect({ label, value, set, disabled }) {
  const options = weekChoices(new Date().toISOString().slice(0, 10), value);
  return <TextField select size="small" label={label} value={value ?? 'none'} disabled={disabled}
    onChange={(e) => set(e.target.value === 'none' ? null : e.target.value)}
    slotProps={{ select: { MenuProps: { slotProps: { paper: { sx: { maxHeight: 280 } } } } } }} sx={{ width: 160 }}>
    <MenuItem value="none">–</MenuItem>
    {options.map((monday) => <MenuItem key={monday} value={monday}>{weekText(monday).replace(/^w/, 'W')}</MenuItem>)}
  </TextField>;
}

// One sheet per card: its fields, then the refusals, then Save.
function EditSheet({ title, phone, close, busy, problems, ok, save, label, left, children }) {
  return <Sheet wide={!phone} onClose={busy ? () => {} : close}
    title={<Typography variant="h6" sx={{ fontWeight: 600 }}>{title}</Typography>}>
    <Stack spacing={2} component="form" onSubmit={(e) => { e.preventDefault(); save(); }}>
      {children}
      <Problems refused={problems} />
      <SheetButtons close={close} save={save} ok={ok} busy={busy} left={left} label={label} />
    </Stack>
  </Sheet>;
}

// --- The sheets of a container's page.

function DetailsSheet({ c, place, locked, csrf, phone, close, done }) {
  const [d, setD] = React.useState(detailsDraft(c));
  const { busy, problems, run, clear } = useRun(done);
  const set = (patch) => { setD((was) => ({ ...was, ...patch })); clear(); };
  const body = detailsBody(c, d);
  const plans = place.assets.find((a) => a.id === d.asset)?.floor_plans ?? [];
  const uses = d.uses === null ? 'whole' : d.uses;
  const first = place.price_books[0]?.id ?? null;
  const payment = d.price_book === null ? 'no' : 'yes';
  const save = () => { if (changed(body)) run(() => changeContainer(csrf, c.id, body), 'Saved.'); };
  return <EditSheet title="Details" phone={phone} close={close} busy={busy} problems={problems} ok={changed(body)} save={save}>
    <Choice label="Asset" value={d.asset} disabled={locked || busy} options={place.assets.map((a) => [a.id, a.name])}
      set={(v) => set({ asset: v, uses: null })} />
    <Choice label="Booking type" value={d.booking_type} disabled={busy} options={place.booking_types.map((t) => [t.id, t.name])}
      set={(v) => set({ booking_type: v })} />
    <Choice label="Uses" value={uses} disabled={locked || busy} options={[['whole', 'Whole asset'], ...plans.map((p) => [p.id, p.name])]}
      set={(v) => set({ uses: v === 'whole' ? null : v })} />
    <Choice label="Payment" value={payment} disabled={busy} set={(v) => set({ price_book: v === 'yes' ? first : null })}
      options={[...(first === null && payment === 'no' ? [] : [['yes', 'Yes']]), ['no', 'No']]} />
    {d.price_book !== null && <Choice label="Price book" value={d.price_book} disabled={busy}
      options={place.price_books.map((b) => [b.id, b.name])} set={(v) => set({ price_book: v })} />}
  </EditSheet>;
}

function TimeSheet({ c, locked, csrf, phone, close, done }) {
  const [d, setD] = React.useState(timeDraft(c));
  const { busy, problems, run, clear } = useRun(done);
  const set = (key) => (v) => { setD((was) => ({ ...was, [key]: v })); clear(); };
  const body = timeBody(c, d);
  const flexible = d.period_type === 'flexible';
  const options = lengthChoices(d);
  const save = () => { if (changed(body)) run(() => changeContainer(csrf, c.id, body), 'Saved.'); };
  const shut = locked || busy;
  return <EditSheet title="Time" phone={phone} close={close} busy={busy} problems={problems} ok={changed(body)} save={save}>
    <Choice label="Period" value={d.period_type} disabled={shut} width={160}
      set={(v) => { setD((was) => ({ ...was, period_type: v, ...(v === 'flexible' ? FLEXIBLE_LENGTHS : {}) })); clear(); }}
      options={[['fixed', 'Fixed'], ['flexible', 'Flexible']]} />
    <Stack direction="row" spacing={1} useFlexGap sx={{ alignItems: 'center', flexWrap: 'wrap', rowGap: 2 }}>
      <TimeSelect label="Starts" value={d.period_start} to={DAY - 15} disabled={shut} set={set('period_start')} />
      <Typography variant="body2">–</Typography>
      <TimeSelect label="Ends" value={d.endTime} disabled={shut} set={set('endTime')} />
      <Choice label="End day" value={d.endDay} disabled={shut} width={140} set={set('endDay')} options={endDayChoices(d.endDay)} />
    </Stack>
    <Box>
      <Typography variant="caption" sx={{ color: 'text.secondary', display: 'block', mb: 0.5 }}>Starts on</Typography>
      <Days value={d.weekdays} set={set('weekdays')} disabled={shut} />
    </Box>
    {flexible && <Stack direction="row" spacing={1} useFlexGap sx={{ flexWrap: 'wrap', rowGap: 2 }}>
      <Choice label="Interval" value={d.interval} disabled={shut} width={110} options={intervalChoices(d.interval)}
        set={(v) => { setD((was) => ({ ...was, interval: v, minimum_length: v, maximum_length: v })); clear(); }} />
      <Choice label="Shortest" value={d.minimum_length} disabled={busy} width={110} options={options(d.minimum_length)}
        set={set('minimum_length')} />
      <Choice label="Longest" value={d.maximum_length} disabled={busy} width={110} options={options(d.maximum_length)}
        set={set('maximum_length')} />
    </Stack>}
    <Stack direction="row" spacing={1} useFlexGap sx={{ alignItems: 'center', flexWrap: 'wrap', rowGap: 2 }}>
      <WeekSelect label="First week" value={d.first_week} disabled={shut} set={set('first_week')} />
      <WeekSelect label="Last week" value={d.last_week} disabled={busy} set={set('last_week')} />
      <Choice label="Every" value={d.every_weeks} disabled={shut} width={120} set={set('every_weeks')} options={everyChoices()} />
    </Stack>
  </EditSheet>;
}

function CategoriesSheet({ c, place, csrf, phone, close, done }) {
  const [d, setD] = React.useState(categoriesDraft(c));
  const { busy, problems, run, clear } = useRun(done);
  const order = place.booking_categories.map((x) => x.id);
  const update = (next) => { setD(next); clear(); };
  const own = (id) => d.own[id] ?? [];
  const setOwn = (id, list) => update({ ...d, own: { ...d.own, [id]: list } });
  const patch = (id, i, change) => setOwn(id, own(id).map((h, j) => (j === i ? { ...h, ...change } : h)));
  const save = () => {
    if (d.ids.length && categoriesChanged(c, d)) run(() => changeContainerCategories(csrf, c.id, categoriesBody(d)), 'Saved.');
  };
  return <EditSheet title="Booking categories" phone={phone} close={close} busy={busy} problems={problems}
    ok={d.ids.length > 0 && categoriesChanged(c, d)} save={save}>
    {place.booking_categories.map((x) => {
      const ticked = d.ids.includes(x.id);
      return <Box key={x.id} sx={{ border: 1, borderColor: 'divider', borderRadius: 1.5, px: 1, py: 0.5 }}>
        <Stack direction="row" sx={{ alignItems: 'center' }}>
          <Checkbox size="small" checked={ticked} disabled={busy} onChange={() => update(tickCategory(d, order, x.id))}
            slotProps={{ input: { 'aria-label': x.name } }} />
          <Typography variant="body2" sx={{ fontWeight: 600, flex: 1 }}>{x.name}</Typography>
          {ticked && <>
            <Typography variant="body2" sx={{ color: 'text.secondary' }}>Own hours</Typography>
            <Switch size="small" checked={own(x.id).length > 0} disabled={busy}
              onChange={(e) => setOwn(x.id, e.target.checked ? [freshHours(c)] : [])} />
          </>}
        </Stack>
        {ticked && own(x.id).map((h, i) => <Stack key={i} spacing={1.75} sx={{ py: 1, pl: 1, borderTop: 1, borderColor: 'divider' }}>
          <Days value={h.weekdays} set={(v) => patch(x.id, i, { weekdays: v })} />
          <Stack direction="row" spacing={1} sx={{ alignItems: 'center' }}>
            <TimeSelect label="From" value={h.start} to={DAY - 15} set={(v) => patch(x.id, i, { start: v })} />
            <Typography variant="body2">–</Typography>
            <TimeSelect label="To" value={h.end} from={15} set={(v) => patch(x.id, i, { end: v })} />
            <Box sx={{ flex: 1 }} />
            <IconButton size="small" aria-label="Remove hours" onClick={() => setOwn(x.id, own(x.id).filter((_, j) => j !== i))}>
              <CloseRounded fontSize="small" /></IconButton>
          </Stack>
        </Stack>)}
        {ticked && own(x.id).length > 0 && <Button size="small" startIcon={<AddRounded />} sx={{ mb: 0.5 }}
          onClick={() => setOwn(x.id, [...own(x.id), freshHours(c)])}>Hours</Button>}
      </Box>;
    })}
  </EditSheet>;
}

function NewSheet({ place, csrf, phone, close, done }) {
  const [d, setD] = React.useState(() => newDraft(place.assets, place.booking_types));
  const { busy, problems, run, clear } = useRun(done);
  const set = (patch) => { setD((was) => ({ ...was, ...patch })); clear(); };
  const plans = place.assets.find((a) => a.id === d.asset)?.floor_plans ?? [];
  const save = () => { if (newReady(d)) run(() => makeContainer(csrf, newBody(d)), 'Created.'); };
  return <EditSheet title="New container" phone={phone} close={close} busy={busy} problems={problems} ok={newReady(d)} save={save}
    label="Create">
    <Choice label="Asset" value={d.asset ?? ''} disabled={busy} options={place.assets.map((a) => [a.id, a.name])}
      set={(v) => set({ asset: v, uses: null })} />
    <Choice label="Booking type" value={d.booking_type ?? ''} disabled={busy} options={place.booking_types.map((t) => [t.id, t.name])}
      set={(v) => set({ booking_type: v })} />
    <Choice label="Period" value={d.period_type} disabled={busy} options={[['fixed', 'Fixed'], ['flexible', 'Flexible']]}
      set={(v) => set({ period_type: v })} />
    <Choice label="Uses" value={d.uses === null ? 'whole' : d.uses} disabled={busy}
      options={[['whole', 'Whole asset'], ...plans.map((p) => [p.id, p.name])]} set={(v) => set({ uses: v === 'whole' ? null : v })} />
  </EditSheet>;
}

// --- The container's page.

function Refused({ title, problems, phone, close }) {
  return <Sheet wide={!phone} onClose={close} title={<Typography variant="h6" sx={{ fontWeight: 600 }}>{title}</Typography>}>
    <Stack spacing={2}>
      <Problems refused={problems} />
      <Stack direction="row" sx={{ justifyContent: 'flex-end' }}><Button onClick={close}>Close</Button></Stack>
    </Stack>
  </Sheet>;
}

function RemoveSheet({ c, csrf, phone, close, done }) {
  const { busy, problems, run } = useRun(done);
  return <Sheet wide={!phone} onClose={busy ? () => {} : close}
    title={<Typography variant="h6" sx={{ fontWeight: 600 }}>Remove container {containerNo(c.number)}?</Typography>}>
    <Stack spacing={2}>
      <Problems refused={problems} />
      <Stack direction="row" spacing={1} sx={{ justifyContent: 'flex-end' }}>
        <Button onClick={close} disabled={busy}>Cancel</Button>
        <Button variant="contained" color="error" disabled={busy} onClick={() => run(() => removeContainer(csrf, c.id), 'Removed.')}>
          Remove</Button>
      </Stack>
    </Stack>
  </Sheet>;
}

function ContainerPage({ c, place, csrf, phone, back, open, done }) {
  const [sheet, setSheet] = React.useState(null);
  const close = () => setSheet(null);
  const locked = c.locked;
  const asset = nameOf(place.assets, c.asset);
  const kind = nameOf(place.booking_types, c.booking_type);
  const status = useRun((data, toast) => { done(data, toast); });
  const changeStatus = (next, toast) => status.run(() => changeContainer(csrf, c.id, { status: next }), toast);
  // A refusal of Enable is shown on a sheet of its own, as the drawing has it.
  const enable = async () => {
    const result = await changeContainer(csrf, c.id, { status: 'enabled' });
    if (result.kind === 'signed-out') { window.location.assign('/login/'); return; }
    if (result.ok) done(result.data, 'Enabled.');
    else setSheet({ kind: 'refused', problems: containerFailure(result) });
  };
  const props = { c, place, locked, csrf, phone, close, done: (data, toast) => { close(); done(data, toast); } };
  const fixed = (label) => <>{label}<Locked on={locked} /></>;
  const edit = (kind_) => <Button size="small" onClick={() => setSheet({ kind: kind_ })}>Edit</Button>;
  const actions = <Stack direction="row" spacing={1}>
    {c.status === 'enabled'
      ? <Button variant="outlined" disabled={status.busy} onClick={() => changeStatus('disabled', 'Disabled.')}>Disable</Button>
      : <Button variant="contained" onClick={enable}>Enable</Button>}
    <Button color="error" disabled={!c.removable} onClick={() => setSheet({ kind: 'remove' })}>Remove</Button>
  </Stack>;
  return <Stack spacing={2}>
    {phone && <Button startIcon={<ArrowBackRounded />} onClick={back} sx={{ alignSelf: 'flex-start' }}>Containers</Button>}
    <Stack direction="row" spacing={1} sx={{ alignItems: 'center', flexWrap: 'wrap', rowGap: 1 }}>
      <Typography component="h1" variant="h5" sx={{ fontWeight: 600 }}>Container {containerNo(c.number)}</Typography>
      <StatusChip c={c} />
      <Box sx={{ flex: 1 }} />
      {!phone && actions}
    </Stack>
    <Typography variant="body2" sx={{ color: 'text.secondary', mt: '4px !important' }}>
      {kind} · {asset} · {daysText(c.weekdays)} · {timesText(c)}</Typography>
    {phone && actions}
    <Problems refused={status.problems} />
    <Box sx={{ display: 'grid', gap: 2, gridTemplateColumns: phone ? '1fr' : '1fr 1fr' }}>
      <Card title="Details" action={edit('details')}>
        <Field label={fixed('Asset')}>{asset}</Field>
        <Field label="Booking type">{kind}</Field>
        <Field label={fixed('Uses')}>{usesText(c, place.assets)}</Field>
        <Field label="Payment">{paymentText(c, place.price_books)}</Field>
        <Field label="To come">{c.to_come ? counted(c.to_come, 'booking line') : 'None'}</Field>
        {c.last_to_come && <Field label="Last to come">
          <Link component="button" variant="body2" underline="hover" onClick={() => open(c.last_to_come.order)}>
            Booking {c.last_to_come.order}</Link>
          {phone ? <br /> : ' · '}{c.last_to_come.date}</Field>}
      </Card>
      <Card title="Time" action={edit('time')}>
        <Field label={fixed('Period')}>{periodTypeWords(c.period_type)}</Field>
        <Field label={fixed('Times')}>{timesText(c)}</Field>
        <Field label={fixed('Starts on')}>{daysText(c.weekdays)}</Field>
        {c.period_type === 'fixed' ? <Field label="Length">{length(periodLength(c))}</Field> : <>
          <Field label={fixed('Interval')}>{length(c.interval)}</Field>
          <Field label="Lengths">{c.minimum_length === c.maximum_length ? length(c.minimum_length)
            : `${length(c.minimum_length)} to ${length(c.maximum_length)}`}</Field>
        </>}
        <Field label="Weeks">{weeksText(c)}</Field>
      </Card>
    </Box>
    <Card flush title={`Booking categories (${c.categories.length})`} action={edit('categories')}>
      <Table size="small" sx={tight}>
        <TableHead><TableRow>
          <TableCell>Category</TableCell><TableCell>Own hours</TableCell>{c.price_book !== null && <TableCell>Price</TableCell>}
          <TableCell align="right">To come</TableCell>
        </TableRow></TableHead>
        <TableBody>
          {c.categories.map((x) => <TableRow key={x.id}>
            <TableCell sx={{ fontWeight: 600 }}>{nameOf(place.booking_categories, x.id)}</TableCell>
            <TableCell sx={wrap}>{ownText(x)}</TableCell>
            {c.price_book !== null && <TableCell>{x.priced ? 'Yes' : <Chip size="small" variant="outlined" color="warning" label="No price" />}</TableCell>}
            <TableCell align="right">{x.to_come}</TableCell>
          </TableRow>)}
        </TableBody>
      </Table>
    </Card>
    {sheet?.kind === 'details' && <DetailsSheet {...props} />}
    {sheet?.kind === 'time' && <TimeSheet {...props} />}
    {sheet?.kind === 'categories' && <CategoriesSheet {...props} />}
    {sheet?.kind === 'refused' && <Refused title="Not enabled" problems={sheet.problems} phone={phone} close={close} />}
    {sheet?.kind === 'remove' && <RemoveSheet {...props} />}
  </Stack>;
}

// --- Every container, by asset.

export default function Containers({ config, phone, again, open, focus, setCrumb }) {
  const { data, failed, loading } = useRead(readContainers, {}, again);
  const [shown, setShown] = useKept(data);
  const [said, setSaid] = React.useState('');
  const [chosen, setChosen] = React.useState(null);
  // Another place can open one container (the price page's link to it).
  React.useEffect(() => { if (focus) setChosen(focus.id); }, [focus]);
  const [asset, setAsset] = React.useState('all');
  const [making, setMaking] = React.useState(false);
  const done = (answer, toast) => { setShown(answer); setSaid(toast); };
  const place = shown;
  const containers = place?.containers ?? [];
  const c = chosen === null ? undefined : containers.find((x) => x.id === chosen);
  useCrumb(setCrumb, c ? [`Container ${containerNo(c.number)}`] : [], () => setChosen(null));
  const toast = <Toast said={said} clear={() => setSaid('')} />;
  const status = <>
    {loading && !shown && <LinearProgress />}
    {failed && !shown && <Alert severity="error">The containers could not be loaded.</Alert>}
  </>;
  if (place && c) {
    return <>
      <ContainerPage key={c.id} c={c} place={place} csrf={config.csrf} phone={phone} open={open} back={() => setChosen(null)}
        done={(answer, toast_) => {
          done(answer, toast_);
          if (!answer.containers.some((x) => x.id === c.id)) setChosen(null);
        }} />
      {toast}
    </>;
  }
  const groups = (place?.assets ?? []).filter((a) => asset === 'all' || a.id === asset)
    .map((a) => [a, containers.filter((x) => x.asset === a.id)]);
  const usesShort = (x) => usesText(x, place.assets).split(' · ')[0];
  const pick = (x) => () => setChosen(x.id);
  return <Stack spacing={2}>
    {status}
    {place && <>
      <Stack direction="row" spacing={1.5} sx={{ alignItems: 'center' }}>
        <TextField select size="small" label="Asset" value={asset} onChange={(e) => setAsset(e.target.value)}
          sx={{ width: phone ? 'auto' : 200, flex: phone ? 1 : 'none' }}>
          <MenuItem value="all">All assets</MenuItem>
          {place.assets.map((a) => <MenuItem key={a.id} value={a.id}>{a.name}</MenuItem>)}
        </TextField>
        {!phone && <Box sx={{ flex: 1 }} />}
        <Button variant="contained" startIcon={<AddRounded />} onClick={() => setMaking(true)} sx={{ flexShrink: 0 }}
          disabled={place.assets.length === 0 || place.booking_types.length === 0}>New container</Button>
      </Stack>
      {phone ? groups.map(([a, list]) => <Stack key={a.id} spacing={1}>
        <Typography variant="subtitle2" component="h2" sx={{ fontWeight: 600, pt: 0.5 }}>{a.name}</Typography>
        {!list.length && <Typography variant="body2" sx={{ color: 'text.secondary' }}>None</Typography>}
        {list.map((x) => <OpenCard key={x.id} open={pick(x)}>
          <Stack direction="row" spacing={1} sx={{ alignItems: 'center' }}>
            <Typography variant="body2" sx={{ fontWeight: 600, flex: 1 }}>{containerNo(x.number)} {nameOf(place.booking_types, x.booking_type)}</Typography>
            {x.status !== 'enabled' && <StatusChip c={x} />}
          </Stack>
          <Typography variant="caption" component="div" sx={{ color: 'text.secondary' }}>
            {periodTypeWords(x.period_type)} · {timesText(x)} · {daysText(x.weekdays)}</Typography>
          <Typography variant="caption" component="div" sx={{ color: 'text.secondary' }}>
            {categoryNames(x, place.booking_categories)} · {usesText(x, place.assets)} · {x.price_book === null ? 'No charge' : 'Paid'}</Typography>
        </OpenCard>)}
      </Stack>) : <Box sx={boxStyle}>
        <Table size="small" sx={tight}>
          <TableHead><TableRow>
            <TableCell>Container</TableCell><TableCell>Booking type</TableCell><TableCell>Period</TableCell><TableCell>Times</TableCell>
            <TableCell>Starts on</TableCell><TableCell>Weeks</TableCell><TableCell>Categories</TableCell><TableCell>Uses</TableCell>
            <TableCell>Payment</TableCell>
          </TableRow></TableHead>
          <TableBody>
            {groups.map(([a, list]) => <React.Fragment key={a.id}>
              <TableRow><TableCell colSpan={9} sx={{ fontWeight: 600, bgcolor: 'action.hover' }}>{a.name}</TableCell></TableRow>
              {!list.length && <TableRow><TableCell colSpan={9} sx={{ color: 'text.secondary' }}>None</TableCell></TableRow>}
              {list.map((x) => <OpenRow key={x.id} open={pick(x)}>
                <TableCell sx={{ fontWeight: 600 }}>{containerNo(x.number)}{x.status !== 'enabled'
                  && <Chip size="small" variant="outlined" label="Disabled" sx={{ display: 'flex', width: 'fit-content', mt: 0.5 }} />}</TableCell>
                <TableCell>{nameOf(place.booking_types, x.booking_type)}</TableCell>
                <TableCell>{periodTypeWords(x.period_type)}</TableCell><TableCell>{timesText(x)}</TableCell>
                <TableCell>{daysText(x.weekdays)}</TableCell><TableCell sx={{ ...wrap, minWidth: 150 }}>{noWeeks(x) ? '–' : weeksText(x)}</TableCell>
                <TableCell sx={wrap}>{categoryNames(x, place.booking_categories)}</TableCell><TableCell>{usesShort(x)}</TableCell>
                <TableCell>{x.price_book === null ? 'No' : 'Yes'}</TableCell>
              </OpenRow>)}
            </React.Fragment>)}
          </TableBody>
        </Table>
      </Box>}
    </>}
    {making && place && <NewSheet place={place} csrf={config.csrf} phone={phone} close={() => setMaking(false)}
      done={(answer, toast_) => { setMaking(false); done(answer, toast_); setChosen(answer.made); }} />}
    {toast}
  </Stack>;
}
