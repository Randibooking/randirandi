// SPDX-License-Identifier: AGPL-3.0-only
import React from 'react';
import {
  Alert, Box, Button, Checkbox, Chip, FormControlLabel, LinearProgress, Stack, Switch, Table, TableBody, TableCell, TableHead, TableRow,
  TextField, Typography,
} from '@mui/material';
import AddRounded from '@mui/icons-material/AddRounded';
import { Sheet } from '../book/AddSheet';
import { changeRole, makeRole, readRoles, removeRole } from './api';
import { Card, OpenCard, OpenRow } from './parts';
import {
  categoryNames, emptyRole, membersWords, nameOk, nameTaken, roleBody, roleRefusals, setupFailure, ticked,
} from './setup';
import { Problems, SheetButtons, Toast, useKept } from './setupParts';
import useRead from './useRead';

// The Roles: a role's booking categories and whether it gives admin access; the standard
// role is the one a member import gives and never gives admin access. The
// server (/admin/roles/...) keeps the rules and answers the roles after every
// change; the drawing's refusals are shown here before a Save is sent.

const tight = { '& td, & th': { whiteSpace: 'nowrap', px: 1.5 } };

function RoleSheet({ role, roles, categories, csrf, phone, onClose, onDone }) {
  const [draft, setDraft] = React.useState(role ?? emptyRole());
  const [refused, setRefused] = React.useState([]);
  const [busy, setBusy] = React.useState(false);
  const [problem, setProblem] = React.useState(null);
  const set = (patch) => { setDraft((d) => ({ ...d, ...patch })); setRefused([]); setProblem(null); };
  const taken = nameTaken(roles, draft.name, role?.id ?? null);
  const body = roleBody(role, draft);
  const order = categories.map((c) => c.id);
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
    if (busy || !nameOk(draft.name) || taken || body === null) return;
    const found = roleRefusals(roles, draft);
    if (found.length) { setRefused(found); return; }
    run(() => (role === null ? makeRole(csrf, body) : changeRole(csrf, role.id, body)), role === null ? 'Created.' : 'Saved.');
  };
  const remove = role && <Button color="error" disabled={role.members > 0 || role.standard || busy}
    onClick={() => run(() => removeRole(csrf, role.id), 'Removed.')}>Remove</Button>;
  return <Sheet wide={!phone} onClose={busy ? () => {} : onClose}
    title={<Typography variant="h6" sx={{ fontWeight: 600 }}>{role ? role.name : 'New role'}</Typography>}>
    <Stack spacing={1.5} component="form" onSubmit={(e) => { e.preventDefault(); save(); }}>
      <TextField size="small" label="Name" value={draft.name} disabled={busy} onChange={(e) => set({ name: e.target.value })}
        slotProps={{ htmlInput: { maxLength: 200 } }} />
      <Box role="group" aria-label="Booking categories">
        <Typography variant="caption" sx={{ color: 'text.secondary' }}>Booking categories</Typography>
        {categories.map((x) => <FormControlLabel key={x.id} sx={{ display: 'flex' }} label={x.name}
          control={<Checkbox size="small" checked={draft.categories.includes(x.id)} disabled={busy}
            onChange={() => set({ categories: ticked(order, draft.categories, x.id) })} />} />)}
      </Box>
      <FormControlLabel label="Admin access" control={<Switch checked={draft.admin_access} disabled={busy}
        onChange={(e) => set({ admin_access: e.target.checked })} />} />
      <FormControlLabel label="Standard role" control={<Switch checked={draft.standard} disabled={busy}
        onChange={(e) => set({ standard: e.target.checked })} />} />
      <Problems refused={[...(taken ? ['Taken'] : []), ...refused]} problem={problem} />
      <SheetButtons close={onClose} save={save} ok={nameOk(draft.name) && !taken && body !== null} busy={busy} left={remove} />
    </Stack>
  </Sheet>;
}

export default function Roles({ config, phone, again }) {
  const { data, failed, loading } = useRead(readRoles, {}, again);
  const [shown, setShown] = useKept(data);
  const [sheet, setSheet] = React.useState(null);
  const [said, setSaid] = React.useState('');
  const close = () => setSheet(null);
  const roles = shown?.roles ?? [];
  const categories = shown?.booking_categories ?? [];
  const done = (answer, toast) => { close(); setShown(answer); setSaid(toast); };
  const standard = <Chip size="small" variant="outlined" label="Standard" sx={{ ml: 1 }} />;
  return <Stack spacing={2}>
    {loading && !shown && <LinearProgress />}
    {failed && !shown && <Alert severity="error">The roles could not be loaded.</Alert>}
    {shown && <>
      <Button variant="contained" startIcon={<AddRounded />} onClick={() => setSheet({ role: null })} sx={{ alignSelf: 'flex-end' }}>New role</Button>
      <Card flush title={`Roles (${roles.length})`}>
        {phone ? roles.map((r) => <Box key={r.id} sx={{ borderTop: 1, borderColor: 'divider' }}>
          <OpenCard flat open={() => setSheet({ role: r })}>
            <Typography variant="body2" sx={{ fontWeight: 600 }}>{r.name}{r.standard && standard}</Typography>
            <Typography variant="caption" component="div" sx={{ color: 'text.secondary' }}>{categoryNames(r, categories)}</Typography>
            <Typography variant="caption" component="div" sx={{ color: 'text.secondary' }}>
              {r.admin_access ? 'Admin access · ' : ''}{membersWords(r.members)}</Typography>
          </OpenCard>
        </Box>) : <Table size="small" sx={tight}>
          <TableHead><TableRow>
            <TableCell>Role</TableCell><TableCell>Booking categories</TableCell><TableCell>Admin access</TableCell>
            <TableCell align="right">Members</TableCell>
          </TableRow></TableHead>
          <TableBody>
            {roles.map((r) => <OpenRow key={r.id} open={() => setSheet({ role: r })}>
              <TableCell sx={{ fontWeight: 600 }}>{r.name}{r.standard && standard}</TableCell>
              <TableCell>{categoryNames(r, categories)}</TableCell><TableCell>{r.admin_access ? 'Yes' : 'No'}</TableCell>
              <TableCell align="right">{r.members}</TableCell>
            </OpenRow>)}
          </TableBody>
        </Table>}
      </Card>
    </>}
    {sheet && shown && <RoleSheet key={sheet.role?.id ?? 'new'} role={sheet.role} roles={roles} categories={categories}
      csrf={config.csrf} phone={phone} onClose={close} onDone={done} />}
    <Toast said={said} clear={() => setSaid('')} />
  </Stack>;
}
