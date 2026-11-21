// SPDX-License-Identifier: AGPL-3.0-only
import React from 'react';
import {
  Alert, Box, Button, Chip, LinearProgress, MenuItem, Stack, Table, TableBody, TableCell, TableHead, TableRow, TextField, Typography,
} from '@mui/material';
import AddRounded from '@mui/icons-material/AddRounded';
import ArrowBackRounded from '@mui/icons-material/ArrowBackRounded';
import { Sheet } from '../book/AddSheet';
import {
  changeGroup, changePlan, changeSpot, makeGroup, makeSpots, readPlan, removeGroup, removePlan, removeSpot,
} from './api';
import {
  LONGEST_WORD, blocksOf, changed, clampCount, counted, groupBody, groupTaken, nameOk, planBody, planName, planNeedsName, planTaken,
  sameSpots, spotBody, spotTaken, spotsOf, titled, wordOk,
} from './assets';
import { useRun } from './assetParts';
import { containerNo } from './containers';
import { Card, Field, OpenRow } from './parts';
import { Problems, SheetButtons, Toast, useKept } from './setupParts';
import useRead from './useRead';

// A floor plan's page.
// Details, the spots as tiles by group, the groups. A spot keeps its number; a
// removed spot leaves a gap, never one a booking holds. A group with spots is
// not removed.

const tight = { '& td, & th': { whiteSpace: 'nowrap', px: 1.5 } };

// The floor plan's name and word; Remove on the left.
function DetailsSheet({ plan, plans, label, csrf, phone, onClose, onDone }) {
  const [name, setName] = React.useState(plan.name);
  const [word, setWord] = React.useState(plan.word);
  const { busy, problem, setProblem, run } = useRun(onDone);
  const taken = planTaken(plans, name, plan.id);
  const needsName = planNeedsName(plans, plan.id) && name.trim() === '';
  const body = planBody(plan, { name, word });
  const ok = !taken && !needsName && wordOk(word) && changed(body);
  const save = () => {
    if (!ok) return;
    run(() => changePlan(csrf, plan.id, body), 'Saved.');
  };
  const remove = <Button color="error" disabled={!plan.removable || busy}
    onClick={() => run(() => removePlan(csrf, plan.id), 'Removed.')}>Remove</Button>;
  return <Sheet wide={!phone} onClose={busy ? () => {} : onClose} title={<Typography variant="h6" sx={{ fontWeight: 600 }}>{label}</Typography>}>
    <Stack spacing={2} component="form" onSubmit={(e) => { e.preventDefault(); save(); }}>
      <TextField size="small" label="Name" value={name} disabled={busy} onChange={(e) => { setName(e.target.value); setProblem(null); }}
        slotProps={{ htmlInput: { maxLength: 200 } }} />
      <TextField size="small" label="Spot word" value={word} disabled={busy} onChange={(e) => setWord(e.target.value)}
        slotProps={{ htmlInput: { maxLength: LONGEST_WORD } }} />
      <Problems refused={[taken && 'Taken', needsName && 'An asset with more than one floor plan names each.'].filter(Boolean)} problem={problem} />
      <SheetButtons close={onClose} save={save} ok={ok} busy={busy} left={remove} />
    </Stack>
  </Sheet>;
}

function GroupPick({ plan, value, set, disabled }) {
  return <TextField select size="small" label="Group" value={value} disabled={disabled} onChange={(e) => set(e.target.value)}>
    <MenuItem value="none">None</MenuItem>
    {plan.groups.map((g) => <MenuItem key={g.id} value={String(g.id)}>{g.name}</MenuItem>)}
  </TextField>;
}

function SpotSheet({ plan, spot, csrf, phone, onClose, onDone }) {
  const [name, setName] = React.useState(spot.name);
  const [group, setGroup] = React.useState(spot.group ? String(spot.group) : 'none');
  const { busy, problem, setProblem, run } = useRun(onDone);
  const taken = spotTaken(plan, name, spot.id);
  const body = spotBody(spot, { name, group });
  const ok = !taken && changed(body);
  const save = () => {
    if (!ok) return;
    run(() => changeSpot(csrf, plan.id, spot.id, body), 'Saved.');
  };
  const remove = <Button color="error" disabled={!spot.removable || busy}
    onClick={() => run(() => removeSpot(csrf, plan.id, spot.id), 'Removed.')}>Remove</Button>;
  return <Sheet wide={!phone} onClose={busy ? () => {} : onClose}
    title={<Typography variant="h6" sx={{ fontWeight: 600 }}>{titled(plan.word)} {spot.number}</Typography>}>
    <Stack spacing={2} component="form" onSubmit={(e) => { e.preventDefault(); save(); }}>
      <TextField size="small" label="Name" value={name} disabled={busy} onChange={(e) => { setName(e.target.value); setProblem(null); }}
        slotProps={{ htmlInput: { maxLength: 200 } }} />
      <GroupPick plan={plan} value={group} set={(v) => { setGroup(v); setProblem(null); }} disabled={busy} />
      <Field label="Bookings to come">{spot.coming}</Field>
      <Problems refused={taken ? [`Taken by ${plan.word} ${taken.number}`] : []} problem={problem} />
      <SheetButtons close={onClose} save={save} ok={ok} busy={busy} left={remove} />
    </Stack>
  </Sheet>;
}

function AddSpotsSheet({ plan, csrf, phone, onClose, onDone }) {
  const [count, setCount] = React.useState(1);
  const [group, setGroup] = React.useState('none');
  const { busy, problem, setProblem, run } = useRun(onDone);
  const last = Math.max(0, ...plan.spots.map((s) => s.number));
  const save = () => run(() => makeSpots(csrf, plan.id, { count, group: group === 'none' ? null : Number(group) }), `Added ${counted(count, plan.word)}.`);
  return <Sheet wide={!phone} onClose={busy ? () => {} : onClose} title={<Typography variant="h6" sx={{ fontWeight: 600 }}>Add {plan.word}s</Typography>}>
    <Stack spacing={2} component="form" onSubmit={(e) => { e.preventDefault(); save(); }}>
      <TextField size="small" type="number" label={titled(`${plan.word}s`)} value={count} disabled={busy}
        onChange={(e) => { setCount(clampCount(e.target.value, plan.most_spots_at_once)); setProblem(null); }} sx={{ width: 120 }} />
      <GroupPick plan={plan} value={group} set={setGroup} disabled={busy} />
      <Field label="Numbers">{last + 1}{count > 1 ? `–${last + count}` : ''}</Field>
      <Problems problem={problem} />
      <SheetButtons close={onClose} save={save} busy={busy} label="Add" />
    </Stack>
  </Sheet>;
}

// A group with its name, word and spots; a spot tapped joins it or leaves it,
// and a spot in another group moves.
function GroupSheet({ plan, group, csrf, phone, onClose, onDone }) {
  const [name, setName] = React.useState(group?.name ?? '');
  const [word, setWord] = React.useState(group?.word ?? 'group');
  const [mine, setMine] = React.useState(() => new Set(spotsOf(plan, group)));
  const { busy, problem, setProblem, run } = useRun(onDone);
  const taken = groupTaken(plan, name, group?.id ?? null);
  const flip = (id) => setMine((was) => {
    const next = new Set(was);
    if (next.has(id)) next.delete(id); else next.add(id);
    return next;
  });
  const other = (s) => s.group && s.group !== group?.id && plan.groups.find((g) => g.id === s.group);
  const body = groupBody(group, { name, word, spots: [...mine] });
  const ok = nameOk(name) && !taken && wordOk(word) && (group === null || name.trim() !== group.name || word.trim() !== group.word
    || !sameSpots(spotsOf(plan, group), [...mine]));
  const save = () => {
    if (!ok) return;
    run(() => (group === null ? makeGroup(csrf, plan.id, body) : changeGroup(csrf, plan.id, group.id, body)), group === null ? 'Created.' : 'Saved.');
  };
  const remove = group && <Button color="error" disabled={!group.removable || busy}
    onClick={() => run(() => removeGroup(csrf, plan.id, group.id), 'Removed.')}>Remove</Button>;
  return <Sheet wide={!phone} onClose={busy ? () => {} : onClose}
    title={<Typography variant="h6" sx={{ fontWeight: 600 }}>{group ? group.name : 'New group'}</Typography>}>
    <Stack spacing={2} component="form" onSubmit={(e) => { e.preventDefault(); save(); }}>
      <TextField size="small" label="Name" value={name} disabled={busy} onChange={(e) => { setName(e.target.value); setProblem(null); }}
        slotProps={{ htmlInput: { maxLength: 200 } }} />
      <TextField size="small" label="Word" value={word} disabled={busy} onChange={(e) => setWord(e.target.value)}
        slotProps={{ htmlInput: { maxLength: LONGEST_WORD } }} />
      <Typography variant="subtitle2">{titled(`${plan.word}s`)} ({mine.size})</Typography>
      <Box sx={{ display: 'flex', flexWrap: 'wrap', gap: 0.75, maxHeight: 260, overflowY: 'auto' }}>
        {[...plan.spots].sort((x, y) => x.number - y.number).map((s) => <Chip key={s.id} size="small" clickable onClick={() => flip(s.id)}
          color={mine.has(s.id) ? 'primary' : 'default'} variant={mine.has(s.id) ? 'filled' : 'outlined'}
          label={`${s.number}${s.name ? ` ${s.name}` : ''}${!mine.has(s.id) && other(s) ? ` · ${other(s).name}` : ''}`} />)}
      </Box>
      <Problems refused={taken ? ['Taken'] : []} problem={problem} />
      <SheetButtons close={onClose} save={save} ok={ok} busy={busy} left={remove} />
    </Stack>
  </Sheet>;
}

function Tile({ spot, onClick, phone }) {
  return <Box component="button" type="button" onClick={onClick} aria-label={`${spot.number}${spot.name ? ` ${spot.name}` : ''}`} sx={{
    width: phone ? 70 : 84, height: phone ? 56 : 64, border: 1, borderColor: 'divider', borderRadius: 1.5, p: 0.75, cursor: 'pointer',
    bgcolor: 'background.default', color: 'text.primary', font: 'inherit', textAlign: 'left', display: 'flex', flexDirection: 'column',
    justifyContent: 'space-between', '&:hover': { borderColor: 'primary.main' },
  }}>
    <Typography variant="body2" sx={{ fontWeight: 600, lineHeight: 1.2 }}>{spot.number}</Typography>
    <Typography variant="caption" noWrap sx={{ color: 'text.secondary', lineHeight: 1.2, maxWidth: '100%' }}>{spot.name}</Typography>
  </Box>;
}

export default function PlanPage({ config, phone, asset, plan: summary, back, onPlace }) {
  const read = React.useCallback(() => readPlan(summary.id), [summary.id]);
  const { data, failed, loading } = useRead(read, {});
  const [shown, setShown] = useKept(data);
  const [sheet, setSheet] = React.useState(null);
  const [said, setSaid] = React.useState('');
  const close = () => setSheet(null);
  const onDone = (answer, toast) => {
    setShown(answer.plan);
    setSheet(null);
    setSaid(toast);
    onPlace(answer.place, '');
  };
  const plans = asset.floor_plans;
  const label = planName(summary, plans);
  const plan = shown;
  const blocks = plan ? blocksOf(plan) : [];
  const props = { plan, csrf: config.csrf, phone, onClose: close, onDone };
  return <Stack spacing={2}>
    {phone && <Button startIcon={<ArrowBackRounded />} onClick={back} sx={{ alignSelf: 'flex-start' }}>{asset.name}</Button>}
    <Typography component="h1" variant="h5" sx={{ fontWeight: 600 }}>{label}</Typography>
    {loading && !plan && <LinearProgress />}
    {failed && !plan && <Alert severity="error">The floor plan could not be loaded.</Alert>}
    {plan && <>
      <Card title="Details" action={<Button size="small" onClick={() => setSheet({ kind: 'details' })}>Edit</Button>}>
        <Field label="Name">{plan.name || '–'}</Field>
        <Field label="Spot word">{plan.word}</Field>
        <Field label={titled(`${plan.word}s`)}>{plan.spots.length}</Field>
        <Field label="Groups">{plan.groups.length}</Field>
        <Field label="Containers">{plan.containers.length ? plan.containers.map((c) => `${containerNo(c.number)} ${c.type}`).join(', ') : '–'}</Field>
      </Card>
      <Card title={`${titled(plan.word)}s (${plan.spots.length})`}
        action={<Button size="small" startIcon={<AddRounded />} onClick={() => setSheet({ kind: 'add' })}>Add</Button>}>
        <Stack spacing={2}>
          {blocks.map((b) => <Box key={b.key}>
            <Stack direction="row" spacing={1} sx={{ alignItems: 'baseline', mb: 0.75 }}>
              <Typography variant="body2" sx={{ fontWeight: 600 }}>{b.group ? b.group.name : 'No group'}</Typography>
              <Typography variant="caption" sx={{ color: 'text.secondary' }}>{counted(b.spots.length, plan.word)}</Typography>
            </Stack>
            <Box sx={{ display: 'flex', flexWrap: 'wrap', gap: 0.75 }}>
              {b.spots.map((s) => <Tile key={s.id} spot={s} phone={phone} onClick={() => setSheet({ kind: 'spot', spot: s })} />)}
              {!b.spots.length && <Typography variant="body2" sx={{ color: 'text.secondary' }}>None</Typography>}
            </Box>
          </Box>)}
        </Stack>
      </Card>
      <Card flush title={`Groups (${plan.groups.length})`}
        action={<Button size="small" startIcon={<AddRounded />} onClick={() => setSheet({ kind: 'group', group: null })}>New</Button>}>
        {!plan.groups.length ? <Typography variant="body2" sx={{ color: 'text.secondary', px: 2, pb: 2 }}>None</Typography>
          : <Table size="small" sx={tight}>
            <TableHead><TableRow>
              {!phone && <TableCell>Group</TableCell>}<TableCell>Name</TableCell>{!phone && <TableCell>Word</TableCell>}
              <TableCell align="right">{titled(`${plan.word}s`)}</TableCell>
            </TableRow></TableHead>
            <TableBody>
              {plan.groups.map((g) => <OpenRow key={g.id} open={() => setSheet({ kind: 'group', group: g })}>
                {!phone && <TableCell>{g.number}</TableCell>}<TableCell sx={{ fontWeight: 600 }}>{g.name}</TableCell>
                {!phone && <TableCell>{g.word}</TableCell>}<TableCell align="right">{g.spots}</TableCell>
              </OpenRow>)}
            </TableBody>
          </Table>}
      </Card>
      {sheet?.kind === 'details' && <DetailsSheet {...props} plans={plans} label={label} />}
      {sheet?.kind === 'spot' && <SpotSheet key={sheet.spot.id} spot={sheet.spot} {...props} />}
      {sheet?.kind === 'add' && <AddSpotsSheet {...props} />}
      {sheet?.kind === 'group' && <GroupSheet key={sheet.group?.id ?? 'new'} group={sheet.group} {...props} />}
    </>}
    <Toast said={said} clear={() => setSaid('')} />
  </Stack>;
}
