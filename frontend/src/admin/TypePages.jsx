// SPDX-License-Identifier: AGPL-3.0-only
import React from 'react';
import {
  Alert, Box, Button, IconButton, LinearProgress, ListItemIcon, MenuItem, Stack, Table, TableBody, TableCell, TableHead, TableRow, TextField,
  Typography,
} from '@mui/material';
import AddRounded from '@mui/icons-material/AddRounded';
import ApartmentRounded from '@mui/icons-material/ApartmentRounded';
import GroupsRounded from '@mui/icons-material/GroupsRounded';
import HomeRounded from '@mui/icons-material/HomeRounded';
import KeyboardArrowDownRounded from '@mui/icons-material/KeyboardArrowDownRounded';
import KeyboardArrowUpRounded from '@mui/icons-material/KeyboardArrowUpRounded';
import PersonRounded from '@mui/icons-material/PersonRounded';
import WorkRounded from '@mui/icons-material/WorkRounded';
import { Sheet } from '../book/AddSheet';
import {
  changeListItem, makeListItem, moveListItem, readTypes, removeListItem,
} from './api';
import { Card, OpenCard, OpenRow, boxStyle } from './parts';
import {
  CATEGORIES, TYPES, containersWords, count, iconWords, itemBody, itemChanged, nameOk, nameTaken, setupFailure, NO_ICON,
} from './setup';
import { Problems, SheetButtons, Toast, useKept } from './setupParts';
import useRead from './useRead';

// The Booking types and categories. Each list in the order members see it, moved with the arrows; a new row goes
// last; a row opens its sheet (name, a type's description, a category's icon)
// with Remove. The server (/admin/types/...) keeps the rules and answers both
// lists after every change.

// The icon of each key a category may have (CATEGORY_ICONS in randirandi/limits.py).
const ICONS = {
  person: PersonRounded, work: WorkRounded, building: ApartmentRounded, home: HomeRounded, group: GroupsRounded,
};
function IconOf({ name, size = 18 }) {
  const Icon = ICONS[name];
  return Icon ? <Icon sx={{ fontSize: size }} /> : null;
}

const tight = { '& td, & th': { whiteSpace: 'nowrap', px: 1.5 } };
// Plain arrows; the theme's filled icon button is too heavy twice a row.
const plain = { bgcolor: 'transparent', color: 'text.primary', '&:hover': { bgcolor: 'action.hover' }, '&.Mui-disabled': { bgcolor: 'transparent', opacity: 0.2 } };

function Arrows({ list, i, move, busy, name }) {
  const go = (direction) => (e) => { e.stopPropagation(); move(list[i], direction); };
  return <Stack direction="row" sx={{ ml: -0.75 }} onClick={(e) => e.stopPropagation()}>
    <IconButton size="small" aria-label={`Move ${name} up`} sx={plain} disabled={busy || i === 0} onClick={go('up')}>
      <KeyboardArrowUpRounded fontSize="small" /></IconButton>
    <IconButton size="small" aria-label={`Move ${name} down`} sx={plain} disabled={busy || i === list.length - 1} onClick={go('down')}>
      <KeyboardArrowDownRounded fontSize="small" /></IconButton>
  </Stack>;
}

// A type or category, new (`row` null) or changed.
function ItemSheet({ kind, list, rows, row, icons, csrf, phone, onClose, onDone }) {
  const [name, setName] = React.useState(row?.name ?? '');
  const [description, setDescription] = React.useState(row?.description ?? '');
  const [icon, setIcon] = React.useState(row?.icon ?? '');
  const [busy, setBusy] = React.useState(false);
  const [problem, setProblem] = React.useState(null);
  const taken = nameTaken(rows, name, row?.id ?? null);
  const body = itemBody(list, row, { name, description, icon });
  const ok = nameOk(name) && !taken && (row === null || itemChanged(body));
  const run = async (call, toast) => {
    if (busy) return;
    setBusy(true);
    setProblem(null);
    const result = await call();
    if (result.kind === 'signed-out') {
      window.location.assign('/login/');
      return;
    }
    setBusy(false);
    if (result.ok) onDone(result.data, toast);
    else setProblem(setupFailure(result));
  };
  const save = () => {
    if (!ok) return;
    run(() => (row === null ? makeListItem(csrf, list, body) : changeListItem(csrf, list, row.id, body)), row === null ? 'Created.' : 'Saved.');
  };
  const remove = row && <Button color="error" disabled={!row.removable || busy}
    onClick={() => run(() => removeListItem(csrf, list, row.id), 'Removed.')}>Remove</Button>;
  return <Sheet wide={!phone} onClose={busy ? () => {} : onClose}
    title={<Typography variant="h6" sx={{ fontWeight: 600 }}>{row ? row.name : `New ${kind}`}</Typography>}>
    <Stack spacing={2} component="form" onSubmit={(e) => { e.preventDefault(); save(); }}>
      <TextField size="small" label="Name" value={name} disabled={busy} onChange={(e) => { setName(e.target.value); setProblem(null); }}
        slotProps={{ htmlInput: { maxLength: 200 } }} />
      {list === TYPES && <TextField size="small" label="Description" value={description} disabled={busy} multiline minRows={2}
        onChange={(e) => setDescription(e.target.value)} />}
      {list === CATEGORIES && <TextField select size="small" label="Icon" value={icon} disabled={busy} onChange={(e) => setIcon(e.target.value)}>
        <MenuItem value="">{NO_ICON}</MenuItem>
        {icons.map((key) => <MenuItem key={key} value={key}>
          <ListItemIcon sx={{ minWidth: 32 }}><IconOf name={key} size={20} /></ListItemIcon>{iconWords(key)}</MenuItem>)}
      </TextField>}
      <Problems refused={taken ? ['Taken'] : []} problem={problem} />
      <SheetButtons close={onClose} save={save} ok={ok} busy={busy} left={remove} />
    </Stack>
  </Sheet>;
}

// One list: arrows, then its columns; a row opens its sheet.
function ListCard({ title, kind, list, rows, icons, columns, line, csrf, phone, move, busy, onDone }) {
  const [sheet, setSheet] = React.useState(null);
  const close = () => setSheet(null);
  const done = (data, toast) => { close(); onDone(data, toast); };
  const nameOf = (x) => <>{list === CATEGORIES && x.icon && <Box component="span" sx={{ display: 'inline-flex', verticalAlign: 'middle', mr: 0.75 }}>
    <IconOf name={x.icon} /></Box>}{x.name}</>;
  return <Card flush title={title} action={<Button size="small" startIcon={<AddRounded />} onClick={() => setSheet({ row: null })}>New</Button>}>
    {phone ? rows.map((x, i) => <Stack key={x.id} direction="row" sx={{ borderTop: 1, borderColor: 'divider', alignItems: 'center' }}>
      <Box sx={{ flex: 1, minWidth: 0 }}>
        <OpenCard flat open={() => setSheet({ row: x })}>
          <Typography variant="body2" sx={{ fontWeight: 600 }}>{nameOf(x)}</Typography>
          <Typography variant="caption" sx={{ color: 'text.secondary' }}>{line(x)}</Typography>
        </OpenCard>
      </Box>
      <Box sx={{ pr: 1 }}><Arrows list={rows} i={i} move={move} busy={busy} name={x.name} /></Box>
    </Stack>) : <Table size="small" sx={tight}>
      <TableHead><TableRow>
        <TableCell padding="checkbox" /><TableCell>Name</TableCell>
        {columns.map(([head]) => <TableCell key={head}>{head}</TableCell>)}
      </TableRow></TableHead>
      <TableBody>
        {rows.map((x, i) => <OpenRow key={x.id} open={() => setSheet({ row: x })}>
          <TableCell padding="checkbox"><Arrows list={rows} i={i} move={move} busy={busy} name={x.name} /></TableCell>
          <TableCell sx={{ fontWeight: 600 }}>{nameOf(x)}</TableCell>
          {columns.map(([head, value]) => <TableCell key={head} sx={{ whiteSpace: 'normal !important' }}>{value(x)}</TableCell>)}
        </OpenRow>)}
      </TableBody>
    </Table>}
    {sheet && <ItemSheet key={sheet.row?.id ?? 'new'} kind={kind} list={list} rows={rows} row={sheet.row} icons={icons}
      csrf={csrf} phone={phone} onClose={close} onDone={done} />}
  </Card>;
}

export default function Types({ config, phone, again }) {
  const { data, failed, loading } = useRead(readTypes, {}, again);
  const [shown, setShown] = useKept(data);
  const [said, setSaid] = React.useState('');
  const [busy, setBusy] = React.useState(false);
  const [problem, setProblem] = React.useState(null);
  const onDone = (answer, toast) => { setShown(answer); setSaid(toast); setProblem(null); };
  // An arrow: asked of the server, which moves the row in the list as it is
  // then and answers both lists.
  const move = (list) => async (row, direction) => {
    if (busy) return;
    setBusy(true);
    setProblem(null);
    const result = await moveListItem(config.csrf, list, row.id, direction);
    if (result.kind === 'signed-out') {
      window.location.assign('/login/');
      return;
    }
    setBusy(false);
    if (result.ok) setShown(result.data);
    else setProblem(setupFailure(result));
  };
  const types = shown?.booking_types ?? [];
  const categories = shown?.booking_categories ?? [];
  const icons = shown?.icons ?? [];
  const typeLine = (t) => [t.description, containersWords(t.containers)].filter(Boolean).join(' · ');
  const categoryLine = (x) => [x.roles.join(', '), containersWords(x.containers)].filter(Boolean).join(' · ');
  return <Stack spacing={2}>
    {loading && !shown && <LinearProgress />}
    {failed && !shown && <Alert severity="error">The lists could not be loaded.</Alert>}
    {problem && <Alert severity="error" variant="outlined">{problem}</Alert>}
    {shown && <Box sx={{ display: 'grid', gap: 2, gridTemplateColumns: phone ? '1fr' : '1fr 1fr', alignItems: 'start' }}>
      <ListCard title="Booking types" kind="booking type" list={TYPES} rows={types} icons={icons} csrf={config.csrf} phone={phone}
        move={move(TYPES)} busy={busy} onDone={onDone} line={typeLine}
        columns={[['Description', (t) => t.description || '–'], ['Containers', (t) => count(t.containers)]]} />
      <ListCard title="Booking categories" kind="booking category" list={CATEGORIES} rows={categories} icons={icons} csrf={config.csrf}
        phone={phone} move={move(CATEGORIES)} busy={busy} onDone={onDone} line={categoryLine}
        columns={[['Roles', (x) => x.roles.join(', ') || '–'], ['Containers', (x) => count(x.containers)]]} />
    </Box>}
    <Toast said={said} clear={() => setSaid('')} />
  </Stack>;
}
