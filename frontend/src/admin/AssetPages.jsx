// SPDX-License-Identifier: AGPL-3.0-only
import React from 'react';
import {
  Alert, Box, Button, Checkbox, Chip, FormControlLabel, LinearProgress, MenuItem, Stack, Tab, Table, TableBody, TableCell, TableHead,
  TableRow, Tabs, TextField, Typography,
} from '@mui/material';
import AddRounded from '@mui/icons-material/AddRounded';
import ArrowBackRounded from '@mui/icons-material/ArrowBackRounded';
import ChevronRightRounded from '@mui/icons-material/ChevronRightRounded';
import { Sheet } from '../book/AddSheet';
import {
  changeAsset, changeSpace, makeAsset, makePlan, makeSpace, readAssets, removeAsset, removeSpace,
} from './api';
import {
  LONGEST_WORD, assetBody, changed, clampCount, counted, nameOk, nameTaken, occupancyOk, offeredWords, periodText, planName, planNeedsName,
  planTaken, spaceBody, weekdaysText, wordOk,
} from './assets';
import { useRun } from './assetParts';
import { containerNo } from './containers';
import { useCrumb } from './crumbs';
import AvailabilityPage from './AvailabilityPages';
import { weekRuns } from './availability';
import PlanPage from './PlanPages';
import { Card, Field, OpenCard, OpenRow, boxStyle } from './parts';
import { Problems, SheetButtons, Toast, useKept } from './setupParts';
import useRead from './useRead';

// The Assets and Spaces tabs, an asset's page with its details, floor plans and
// containers, a floor plan's page (PlanPages.jsx) and its availability page
// (AvailabilityPages.jsx, part 7.98); the Containers list is read-only
// (its editing is part 7.96). The server (/admin/assets/...) keeps the rules and
// answers the whole place after every change.

const tight = { '& td, & th': { whiteSpace: 'nowrap', px: 1.5 } };

function OfferedChip({ asset }) {
  const yes = asset.offered === 'yes';
  return <Chip size="small" variant="outlined" color={yes ? 'success' : 'default'} label={offeredWords(asset)} />;
}

// A space, new (`row` null) or changed.
function SpaceSheet({ row, data, csrf, phone, onClose, onDone }) {
  const [name, setName] = React.useState(row?.name ?? '');
  const [description, setDescription] = React.useState(row?.description ?? '');
  const [page, setPage] = React.useState(row?.booking_page ?? 'one_asset');
  const { busy, problem, setProblem, run } = useRun(onDone);
  const taken = nameTaken(data.spaces, name, row?.id ?? null);
  const body = spaceBody(row, { name, description, booking_page: page });
  const ok = nameOk(name) && !taken && (row === null || changed(body));
  const save = () => {
    if (!ok) return;
    run(() => (row === null ? makeSpace(csrf, body) : changeSpace(csrf, row.id, body)), row === null ? 'Created.' : 'Saved.');
  };
  const remove = row && <Button color="error" disabled={!row.removable || busy}
    onClick={() => run(() => removeSpace(csrf, row.id), 'Removed.')}>Remove</Button>;
  return <Sheet wide={!phone} onClose={busy ? () => {} : onClose}
    title={<Typography variant="h6" sx={{ fontWeight: 600 }}>{row ? row.name : 'New space'}</Typography>}>
    <Stack spacing={2} component="form" onSubmit={(e) => { e.preventDefault(); save(); }}>
      <TextField size="small" label="Name" value={name} disabled={busy} onChange={(e) => { setName(e.target.value); setProblem(null); }}
        slotProps={{ htmlInput: { maxLength: 200 } }} />
      <TextField size="small" label="Description" value={description} disabled={busy} multiline minRows={2}
        onChange={(e) => setDescription(e.target.value)} />
      <TextField select size="small" label="Booking page" value={page} disabled={busy} onChange={(e) => setPage(e.target.value)}>
        {data.booking_pages.map((p) => <MenuItem key={p.value} value={p.value}>{p.label}</MenuItem>)}
      </TextField>
      <Problems refused={taken ? ['Taken'] : []} problem={problem} />
      <SheetButtons close={onClose} save={save} ok={ok} busy={busy} left={remove} />
    </Stack>
  </Sheet>;
}

// An asset, new (`row` null) or changed.
function AssetSheet({ row, data, csrf, phone, onClose, onDone }) {
  const [name, setName] = React.useState(row?.name ?? '');
  const [description, setDescription] = React.useState(row?.description ?? '');
  const [space, setSpace] = React.useState(row?.space ? String(row.space) : '');
  const [whole, setWhole] = React.useState(row?.whole_space ?? false);
  const [occupancy, setOccupancy] = React.useState(row?.maximum_occupancy ? String(row.maximum_occupancy) : '');
  const [zone, setZone] = React.useState(row && row.time_zone !== data.time_zone ? row.time_zone : '');
  const { busy, problem, setProblem, run } = useRun(onDone);
  const taken = nameTaken(data.assets, name, row?.id ?? null);
  const draft = { name, description, space, whole_space: whole, maximum_occupancy: occupancy, time_zone: zone };
  const body = assetBody(row, draft, data.time_zone);
  const ok = nameOk(name) && !taken && occupancyOk(occupancy) && (row === null || changed(body));
  const save = () => {
    if (!ok) return;
    run(() => (row === null ? makeAsset(csrf, body) : changeAsset(csrf, row.id, body)), row === null ? 'Created.' : 'Saved.');
  };
  const remove = row && <Button color="error" disabled={!row.removable || busy}
    onClick={() => run(() => removeAsset(csrf, row.id), 'Removed.')}>Remove</Button>;
  return <Sheet wide={!phone} onClose={busy ? () => {} : onClose}
    title={<Typography variant="h6" sx={{ fontWeight: 600 }}>{row ? row.name : 'New asset'}</Typography>}>
    <Stack spacing={2} component="form" onSubmit={(e) => { e.preventDefault(); save(); }}>
      <TextField size="small" label="Name" value={name} disabled={busy} onChange={(e) => { setName(e.target.value); setProblem(null); }}
        slotProps={{ htmlInput: { maxLength: 200 } }} />
      <TextField size="small" label="Description" value={description} disabled={busy} multiline minRows={2}
        onChange={(e) => setDescription(e.target.value)} />
      <TextField select size="small" label="Space" value={space} disabled={busy}
        onChange={(e) => { setSpace(e.target.value); if (e.target.value === '') setWhole(false); setProblem(null); }}>
        <MenuItem value="">None</MenuItem>
        {data.spaces.map((x) => <MenuItem key={x.id} value={String(x.id)}>{x.name}</MenuItem>)}
      </TextField>
      <FormControlLabel disabled={busy || space === ''} label="Whole space"
        control={<Checkbox checked={whole && space !== ''} onChange={(e) => { setWhole(e.target.checked); setProblem(null); }} />} />
      <TextField size="small" label="Max occupancy" value={occupancy} disabled={busy} error={!occupancyOk(occupancy)}
        onChange={(e) => setOccupancy(e.target.value)} slotProps={{ htmlInput: { inputMode: 'numeric' } }} sx={{ width: 160 }} />
      <TextField select size="small" label="Time zone" value={zone} disabled={busy} onChange={(e) => { setZone(e.target.value); setProblem(null); }}>
        <MenuItem value="">{data.time_zone}</MenuItem>
        {data.time_zones.filter((z) => z !== data.time_zone).map((z) => <MenuItem key={z} value={z}>{z}</MenuItem>)}
      </TextField>
      <Problems refused={taken ? ['Taken'] : []} problem={problem} />
      <SheetButtons close={onClose} save={save} ok={ok} busy={busy} left={remove} />
    </Stack>
  </Sheet>;
}

// A floor plan new on an asset: a name (needed beside another), the spot word and the spots.
function NewPlanSheet({ asset, data, csrf, phone, onClose, onDone }) {
  const [name, setName] = React.useState('');
  const [word, setWord] = React.useState('spot');
  const [spots, setSpots] = React.useState(1);
  const { busy, problem, setProblem, run } = useRun(onDone);
  const plans = asset.floor_plans;
  const taken = planTaken(plans, name, null);
  // The asset's only floor plan has no name yet: it is named first (the rule is
  // the model's, asset_inventory: an asset with more than one names each).
  const unnamed = plans.some((p) => p.name === '');
  const needsName = planNeedsName(plans, null) && name.trim() === '';
  const ok = !taken && !needsName && !unnamed && wordOk(word);
  const save = () => {
    if (!ok) return;
    run(() => makePlan(csrf, asset.id, { name: name.trim(), word: word.trim(), spots }), 'Created.');
  };
  return <Sheet wide={!phone} onClose={busy ? () => {} : onClose} title={<Typography variant="h6" sx={{ fontWeight: 600 }}>New floor plan</Typography>}>
    <Stack spacing={2} component="form" onSubmit={(e) => { e.preventDefault(); save(); }}>
      <TextField size="small" label="Name" value={name} disabled={busy} onChange={(e) => { setName(e.target.value); setProblem(null); }}
        slotProps={{ htmlInput: { maxLength: 200 } }} />
      <TextField size="small" label="Spot word" value={word} disabled={busy} onChange={(e) => setWord(e.target.value)}
        slotProps={{ htmlInput: { maxLength: LONGEST_WORD } }} />
      <TextField size="small" type="number" label="Spots" value={spots} disabled={busy}
        onChange={(e) => setSpots(clampCount(e.target.value, data.most_spots_at_once))} sx={{ width: 120 }} />
      <Problems refused={[taken && 'Taken', unnamed && 'Name the first floor plan before adding another.'].filter(Boolean)} problem={problem} />
      <SheetButtons close={onClose} save={save} ok={ok} busy={busy} />
    </Stack>
  </Sheet>;
}

function FloorPlans({ asset, data, csrf, phone, open, onDone }) {
  const [making, setMaking] = React.useState(false);
  const plans = asset.floor_plans;
  const line = (p) => [counted(p.spots, p.word), counted(p.groups, 'group'), counted(p.containers, 'container')].join(' · ');
  return <Card flush title={`Floor plans (${plans.length})`}
    action={<Button size="small" startIcon={<AddRounded />} onClick={() => setMaking(true)}>New</Button>}>
    {phone ? plans.map((p) => <OpenCard flat key={p.id} open={() => open(p.id)}>
      <Typography variant="body2" sx={{ fontWeight: 600 }}>{planName(p, plans)}</Typography>
      <Typography variant="caption" sx={{ color: 'text.secondary' }}>{line(p)}</Typography>
    </OpenCard>) : <Table size="small" sx={tight}>
      <TableHead><TableRow>
        <TableCell>Floor plan</TableCell><TableCell>Spot word</TableCell><TableCell align="right">Spots</TableCell>
        <TableCell align="right">Groups</TableCell><TableCell align="right">Containers</TableCell>
      </TableRow></TableHead>
      <TableBody>
        {plans.map((p) => <OpenRow key={p.id} open={() => open(p.id)}>
          <TableCell sx={{ fontWeight: 600 }}>{planName(p, plans)}</TableCell><TableCell>{p.word}</TableCell>
          <TableCell align="right">{p.spots}</TableCell><TableCell align="right">{p.groups}</TableCell>
          <TableCell align="right">{p.containers}</TableCell>
        </OpenRow>)}
      </TableBody>
    </Table>}
    {making && <NewPlanSheet asset={asset} data={data} csrf={csrf} phone={phone} onClose={() => setMaking(false)}
      onDone={(answer, toast) => { setMaking(false); onDone(answer, toast); }} />}
  </Card>;
}

// The asset's containers, as the drawing lists them; their editing is part 7.96.
function Containers({ asset, phone, toContainer }) {
  const list = asset.containers;
  const categories = (c) => c.categories.join(', ');
  const uses = (c) => (c.mode === 'exclusive' ? 'Whole asset'
    : planName(asset.floor_plans.find((p) => p.id === c.floor_plan) ?? { id: 0, name: '' }, asset.floor_plans));
  return <Card flush title={`Containers (${list.length})`}>
    {phone ? list.map((c) => <OpenCard flat key={c.id} open={() => toContainer(c.id)}>
      <Typography variant="body2" sx={{ fontWeight: 600 }}>{containerNo(c.number)} {c.type}</Typography>
      <Typography variant="caption" sx={{ color: 'text.secondary' }}>
        {periodText(c)} · {weekdaysText(c)} · {categories(c)} · {c.payment ? 'Paid' : 'No charge'}</Typography>
    </OpenCard>) : <Table size="small" sx={tight}>
      <TableHead><TableRow>
        <TableCell>Container</TableCell><TableCell>Booking type</TableCell><TableCell>Period</TableCell><TableCell>Times</TableCell>
        <TableCell>Weekdays</TableCell><TableCell>Categories</TableCell><TableCell>Uses</TableCell><TableCell>Payment</TableCell>
      </TableRow></TableHead>
      <TableBody>
        {list.map((c) => <OpenRow key={c.id} open={() => toContainer(c.id)}>
          <TableCell sx={{ fontWeight: 600 }}>{containerNo(c.number)}</TableCell><TableCell>{c.type}</TableCell>
          <TableCell>{c.period === 'fixed' ? 'Fixed' : 'Flexible'}</TableCell><TableCell>{periodText(c)}</TableCell>
          <TableCell>{weekdaysText(c)}</TableCell><TableCell>{categories(c)}</TableCell><TableCell>{uses(c)}</TableCell>
          <TableCell>{c.payment ? 'Yes' : 'No'}</TableCell>
        </OpenRow>)}
        {!list.length && <TableRow><TableCell colSpan={8} sx={{ color: 'text.secondary', textAlign: 'center', py: 3 }}>None</TableCell></TableRow>}
      </TableBody>
    </Table>}
  </Card>;
}

// The asset's availability in short, with the way to its page.
function AvailabilityCard({ asset, open }) {
  const short = asset.availability;
  const coming = short.closures_coming;
  return <Card title="Availability" action={<Button size="small" endIcon={<ChevronRightRounded />} onClick={open}>Open</Button>}>
    <Field label="In force">{short.in_force ? `From ${short.in_force.from}` : '–'}</Field>
    {short.in_force && weekRuns(short.in_force.week).map((r, i) => <Field key={r} label={i ? '' : 'Week'}>{r}</Field>)}
    <Field label="Next">{short.next ? `From ${short.next}` : '–'}</Field>
    <Field label="Closures">{coming ? `${coming} to come` : 'None to come'}</Field>
  </Card>;
}

function AssetPage({ asset, data, csrf, phone, back, toContainer, openPlan, openAvailability, edit, onDone }) {
  return <Stack spacing={2}>
    {phone && <Button startIcon={<ArrowBackRounded />} onClick={back} sx={{ alignSelf: 'flex-start' }}>Assets</Button>}
    <Stack direction="row" spacing={1} sx={{ alignItems: 'center' }}>
      <Typography component="h1" variant="h5" sx={{ fontWeight: 600 }}>{asset.name}</Typography>
      <OfferedChip asset={asset} />
    </Stack>
    <Box sx={{ display: 'grid', gap: 2, gridTemplateColumns: phone ? '1fr' : '1fr 1fr', alignItems: 'start' }}>
      <Card title="Details" action={<Button size="small" onClick={edit}>Edit</Button>}>
        <Field label="Name">{asset.name}</Field>
        <Field label="Description">{asset.description || '–'}</Field>
        <Field label="Time zone">{asset.time_zone}</Field>
        <Field label="Space">{asset.space_name || '–'}</Field>
        <Field label="Whole space">{asset.whole_space ? 'Yes' : 'No'}</Field>
        <Field label="Max occupancy">{asset.maximum_occupancy ?? '–'}</Field>
      </Card>
      <AvailabilityCard asset={asset} open={openAvailability} />
    </Box>
    <FloorPlans asset={asset} data={data} csrf={csrf} phone={phone} open={openPlan} onDone={onDone} />
    <Containers asset={asset} phone={phone} toContainer={toContainer} />
  </Stack>;
}

function AssetList({ data, phone, open, add }) {
  const plans = (a) => counted(a.floor_plans.length, 'floor plan');
  return <Stack spacing={2}>
    <Button variant="contained" startIcon={<AddRounded />} sx={{ alignSelf: 'flex-end' }} onClick={add}>New asset</Button>
    {phone ? data.assets.map((a) => <OpenCard key={a.id} open={() => open(a.id)}>
      <Stack direction="row" spacing={1} sx={{ alignItems: 'center' }}>
        <Typography variant="body2" sx={{ fontWeight: 600, flex: 1 }}>{a.name}</Typography>
        <OfferedChip asset={a} />
      </Stack>
      <Typography variant="caption" sx={{ color: 'text.secondary' }}>
        {a.space_name ? `${a.space_name} · ` : ''}{plans(a)} · {counted(a.containers.length, 'container')}</Typography>
    </OpenCard>) : <Box sx={boxStyle}>
      <Table size="small" sx={tight}>
        <TableHead><TableRow>
          <TableCell>Asset</TableCell><TableCell>Space</TableCell><TableCell>Whole space</TableCell>
          <TableCell align="right">Floor plans</TableCell><TableCell align="right">Containers</TableCell>
          <TableCell align="right">Max occupancy</TableCell><TableCell>Offered</TableCell>
        </TableRow></TableHead>
        <TableBody>
          {data.assets.map((a) => <OpenRow key={a.id} open={() => open(a.id)}>
            <TableCell sx={{ fontWeight: 600 }}>{a.name}</TableCell><TableCell>{a.space_name || '–'}</TableCell>
            <TableCell>{a.whole_space ? 'Yes' : 'No'}</TableCell>
            <TableCell align="right">{a.floor_plans.length}</TableCell><TableCell align="right">{a.containers.length}</TableCell>
            <TableCell align="right">{a.maximum_occupancy ?? '–'}</TableCell><TableCell>{offeredWords(a)}</TableCell>
          </OpenRow>)}
        </TableBody>
      </Table>
    </Box>}
  </Stack>;
}

function SpaceList({ data, phone, open, add }) {
  const pageWords = (x) => data.booking_pages.find((p) => p.value === x.booking_page)?.label ?? x.booking_page;
  return <Stack spacing={2}>
    <Button variant="contained" startIcon={<AddRounded />} sx={{ alignSelf: 'flex-end' }} onClick={add}>New space</Button>
    {phone ? data.spaces.map((x) => <OpenCard key={x.id} open={() => open(x)}>
      <Typography variant="body2" sx={{ fontWeight: 600 }}>{x.name}</Typography>
      <Typography variant="caption" sx={{ color: 'text.secondary' }}>{pageWords(x)} · {counted(x.assets, 'asset')}</Typography>
    </OpenCard>) : <Box sx={boxStyle}>
      <Table size="small" sx={tight}>
        <TableHead><TableRow><TableCell>Space</TableCell><TableCell>Booking page</TableCell><TableCell align="right">Assets</TableCell></TableRow></TableHead>
        <TableBody>
          {!data.spaces.length && <TableRow><TableCell colSpan={3} sx={{ color: 'text.secondary', py: 4, textAlign: 'center' }}>None</TableCell></TableRow>}
          {data.spaces.map((x) => <OpenRow key={x.id} open={() => open(x)}>
            <TableCell sx={{ fontWeight: 600 }}>{x.name}</TableCell><TableCell>{pageWords(x)}</TableCell>
            <TableCell align="right">{x.assets}</TableCell>
          </OpenRow>)}
        </TableBody>
      </Table>
    </Box>}
  </Stack>;
}

export default function Assets({ config, phone, again, toContainer, setCrumb }) {
  // `reread` is raised when a page under an asset changed what the place shows.
  const [reread, setReread] = React.useState(0);
  const { data, failed, loading } = useRead(readAssets, {}, again + reread);
  const [shown, setShown] = useKept(data);
  const [tab, setTab] = React.useState('assets');
  const [asset, setAsset] = React.useState(null);
  const [plan, setPlan] = React.useState(null);
  const [availability, setAvailability] = React.useState(false);
  const [sheet, setSheet] = React.useState(null);
  const [said, setSaid] = React.useState('');
  const close = () => setSheet(null);
  // A change answers the place; the sheet closes and a toast says so.
  const onDone = (answer, toast) => { setShown(answer.place); setSheet(null); setSaid(toast); };
  const row = shown && asset !== null ? shown.assets.find((a) => a.id === asset) : null;
  // An asset removed, or gone, leaves its page.
  React.useEffect(() => { if (shown && asset !== null && !row) { setAsset(null); setPlan(null); setAvailability(false); } }, [shown, asset, row]);
  const planRow = row && plan !== null ? row.floor_plans.find((p) => p.id === plan) : null;
  React.useEffect(() => { if (row && plan !== null && !planRow) setPlan(null); }, [row, plan, planRow]);
  // The breadcrumb: the asset, and under it the floor plan or the availability open.
  useCrumb(setCrumb, row ? [row.name, ...(availability ? ['Availability'] : planRow ? [planName(planRow, row.floor_plans)] : [])] : [],
    (index) => {
      if (index === 0) setAsset(null);
      setPlan(null);
      setAvailability(false);
    });
  const props = { data: shown, csrf: config.csrf, phone, onClose: close };
  let body = null;
  if (shown && row && availability) {
    body = <AvailabilityPage key={row.id} config={config} phone={phone} asset={row} back={() => setAvailability(false)}
      onChanged={() => setReread((n) => n + 1)} />;
  } else if (shown && row && planRow) {
    body = <PlanPage key={planRow.id} config={config} phone={phone} asset={row} plan={planRow} back={() => setPlan(null)}
      onPlace={(place, toast) => { setShown(place); if (toast) setSaid(toast); }} />;
  } else if (shown && row) {
    body = <AssetPage asset={row} data={shown} csrf={config.csrf} phone={phone} back={() => setAsset(null)} toContainer={toContainer} openPlan={setPlan} openAvailability={() => setAvailability(true)}
      edit={() => setSheet({ kind: 'asset', row })} onDone={onDone} />;
  } else if (shown) {
    body = <Stack spacing={2}>
      <Tabs value={tab} onChange={(_, v) => setTab(v)} sx={{ borderBottom: 1, borderColor: 'divider', minHeight: 40, '& .MuiTab-root': { minHeight: 40 } }}>
        <Tab value="assets" label="Assets" />
        <Tab value="spaces" label="Spaces" />
      </Tabs>
      {tab === 'assets'
        ? <AssetList data={shown} phone={phone} open={setAsset} add={() => setSheet({ kind: 'asset', row: null })} />
        : <SpaceList data={shown} phone={phone} open={(x) => setSheet({ kind: 'space', row: x })} add={() => setSheet({ kind: 'space', row: null })} />}
    </Stack>;
  }
  return <Stack spacing={2}>
    {loading && !shown && <LinearProgress />}
    {failed && !shown && <Alert severity="error">The assets could not be loaded.</Alert>}
    {body}
    {sheet?.kind === 'space' && <SpaceSheet key={sheet.row?.id ?? 'new'} row={sheet.row} {...props} onDone={(answer, toast) => onDone(answer, toast)} />}
    {sheet?.kind === 'asset' && <AssetSheet key={sheet.row?.id ?? 'new'} row={sheet.row} {...props} onDone={(answer, toast) => onDone(answer, toast)} />}
    <Toast said={said} clear={() => setSaid('')} />
  </Stack>;
}
