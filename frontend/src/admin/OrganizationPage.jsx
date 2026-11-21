// SPDX-License-Identifier: AGPL-3.0-only
import React from 'react';
import {
  Alert, Autocomplete, Box, Button, InputAdornment, LinearProgress, MenuItem, Stack, TextField, Typography,
} from '@mui/material';
import EditRounded from '@mui/icons-material/EditRounded';
import UnfoldMoreRounded from '@mui/icons-material/UnfoldMoreRounded';
import { Sheet } from '../book/AddSheet';
import { changeOrganization, readOrganization } from './api';
import { Card, Field } from './parts';
import {
  daysWords, draftOf, eraseWords, failure, limitMonthsWords, limitWords, monthsWords, readDraft, saveBody, taxWords,
} from './organization';
import { Problems, SheetButtons, Toast, useKept } from './setupParts';
import useRead from './useRead';

// The Organization, in the pattern of the other Setup places (Roles, Booking
// types): the details as a summary, and one Details sheet to change them. No
// drawing of it exists in the operator's demo. The server
// (/admin/organization/...) keeps the rules and answers the place after every
// save. The organization's own words (labels) and its subdomain are not
// changed here.

function Summary({ place, phone, open }) {
  const standard = place.roles.find((r) => r.id === place.standard_role);
  const language = place.languages.find((l) => l.code === place.language);
  return <Stack spacing={2}>
    <Button variant="contained" startIcon={<EditRounded />} onClick={open} sx={{ alignSelf: 'flex-end' }}>Details</Button>
    <Box sx={{ display: 'grid', gap: 2, gridTemplateColumns: phone ? '1fr' : '1fr 1fr', alignItems: 'start' }}>
      <Card title="Organization">
        <Field label="Name">{place.name}</Field>
        <Field label="Subdomain">{place.subdomain}</Field>
        <Field label="Contact email">{place.contact_email}</Field>
        <Field label="Free cancellation">{daysWords(place.free_cancellation_days)}</Field>
        <Field label="Standard role">{standard?.name ?? '–'}</Field>
        <Field label="Language">{language?.name ?? place.language}</Field>
      </Card>
      <Card title="Booking limits">
        <Field label="Bookings a day">{limitWords(place.max_bookings_a_day)}</Field>
        <Field label="Book ahead">{limitMonthsWords(place.book_ahead_months)}</Field>
      </Card>
      <Card title="Data kept">
        <Field label="Members">{monthsWords(place.member_retention_months)}</Field>
        <Field label="Bookings">{monthsWords(place.booking_retention_months)}</Field>
      </Card>
      <Card title="Prices and defaults">
        <Field label="Tax rate">{taxWords(place.tax_rate)}</Field>
        <Field label="Currency">{place.currency}</Field>
        <Field label="Time zone">{place.time_zone}</Field>
      </Card>
    </Box>
  </Stack>;
}

function Erase({ erase, busy, back, confirm, code, setCode, problem }) {
  return <Stack spacing={2} component="form" onSubmit={(e) => { e.preventDefault(); if (code.trim()) confirm(); }}>
    <Alert severity="warning" variant="outlined">{eraseWords(erase)}</Alert>
    <TextField size="small" fullWidth label="Code from your authenticator app" placeholder="123 456" value={code} disabled={busy}
      onChange={(e) => setCode(e.target.value)} autoComplete="one-time-code" slotProps={{ htmlInput: { inputMode: 'numeric' } }} />
    <Problems problem={problem} />
    <Stack direction="row" spacing={1} sx={{ justifyContent: 'flex-end' }}>
      <Button onClick={back} disabled={busy}>Back</Button>
      <Button type="submit" variant="contained" color="error" disabled={busy || code.trim() === ''}>Erase and save</Button>
    </Stack>
  </Stack>;
}

function DetailsSheet({ place, csrf, phone, onClose, onDone, onStale }) {
  const [draft, setDraft] = React.useState(() => draftOf(place));
  const [faults, setFaults] = React.useState({});
  const [busy, setBusy] = React.useState(false);
  const [problem, setProblem] = React.useState(null);
  const [erase, setErase] = React.useState(null);
  const [code, setCode] = React.useState('');
  const set = (patch) => { setDraft((d) => ({ ...d, ...patch })); setFaults({}); setProblem(null); };
  const body = saveBody(place, draft);
  const found = readDraft(place, draft).faults;
  const send = async (confirm) => {
    if (busy || body === null) return;
    setBusy(true);
    setProblem(null);
    const result = await changeOrganization(csrf, confirm ? { ...body, confirm, code } : body);
    if (result.kind === 'signed-out') {
      window.location.assign('/login/');
      return;
    }
    setBusy(false);
    if (result.ok) { onDone(result.data); return; }
    if (result.code === 'confirm-erasure' && result.body?.erase) { setCode(''); setErase(result.body.erase); return; }
    if (result.code === 'stale') { onStale(); return; }
    const said = failure(result);
    // A refused code leaves the question open; the code is used once, so it is emptied.
    if (result.code === 'code-refused' || result.code === 'code-limited') { setCode(''); setProblem(said.text); return; }
    if (said.field) setFaults({ [said.field]: said.text });
    else setProblem(said.text);
    setErase(null);
  };
  // A lowered retention is answered by the server with what the next run would
  // erase; it is saved only when that is sent back (Erase and save).
  const save = () => send(null);
  const shown = (key) => faults[key] ?? (draft[key] !== draftOf(place)[key] ? found[key] : undefined);
  const field = (key, label, extra = {}) => <TextField size="small" label={label} value={draft[key]} disabled={busy}
    onChange={(e) => set({ [key]: e.target.value })} error={!!shown(key)} helperText={shown(key)} {...extra} />;
  const unit = (key, label, word, mode = 'numeric') => field(key, label, {
    slotProps: { htmlInput: { inputMode: mode }, input: { endAdornment: <InputAdornment position="end">{word}</InputAdornment> } },
  });
  const row = { direction: phone ? 'column' : 'row', spacing: 1.5 };
  const roles = place.roles.filter((r) => !r.admin_access || r.id === place.standard_role);
  return <Sheet wide={!phone} onClose={busy ? () => {} : onClose}
    title={<Typography variant="h6" sx={{ fontWeight: 600 }}>{erase ? 'Erase data?' : 'Details'}</Typography>}>
    {erase ? <Erase erase={erase} busy={busy} back={() => { setErase(null); setProblem(null); }} confirm={() => send(erase)} code={code}
      setCode={setCode} problem={problem} />
      : <Stack spacing={1.5} component="form" onSubmit={(e) => { e.preventDefault(); save(); }}>
        {field('name', 'Name', { slotProps: { htmlInput: { maxLength: 200 } } })}
        <TextField size="small" label="Subdomain" value={place.subdomain} disabled />
        {field('contact_email', 'Contact email', { type: 'email' })}
        {unit('free_cancellation_days', 'Free cancellation', 'days')}
        <TextField size="small" select label="Standard role" value={draft.standard_role} disabled={busy}
          onChange={(e) => set({ standard_role: e.target.value })} error={!!shown('standard_role')} helperText={shown('standard_role')}>
          {roles.map((r) => <MenuItem key={r.id} value={String(r.id)}>{r.name}</MenuItem>)}
        </TextField>
        <TextField size="small" select label="Language" value={draft.language} disabled={busy}
          onChange={(e) => set({ language: e.target.value })} error={!!shown('language')} helperText={shown('language')}>
          {place.languages.map((l) => <MenuItem key={l.code} value={l.code}>{l.name}</MenuItem>)}
        </TextField>
        <Stack {...row}>
          {field('max_bookings_a_day', 'Bookings a day', { slotProps: { htmlInput: { inputMode: 'numeric' } } })}
          {unit('book_ahead_months', 'Book ahead', 'months')}
        </Stack>
        {unit('tax_rate', 'Tax rate', '%', 'decimal')}
        <Stack {...row}>
          {unit('member_retention_months', 'Member retention', 'months')}
          {unit('booking_retention_months', 'Booking retention', 'months')}
        </Stack>
        <Autocomplete size="small" disableClearable options={place.time_zones} value={draft.time_zone} disabled={busy}
          popupIcon={<UnfoldMoreRounded fontSize="small" />}
          slotProps={{ popupIndicator: { sx: { border: 0, bgcolor: 'transparent', '&:hover': { border: 0, bgcolor: 'transparent' } } } }}
          onChange={(_, value) => set({ time_zone: value })}
          renderInput={(params) => <TextField {...params} label="Time zone" error={!!shown('time_zone')} helperText={shown('time_zone')} />} />
        <TextField size="small" select label="Currency" value={draft.currency} disabled={busy}
          onChange={(e) => set({ currency: e.target.value })} error={!!shown('currency')} helperText={shown('currency')}>
          {place.currencies.map((c) => <MenuItem key={c} value={c}>{c}</MenuItem>)}
        </TextField>
        <Problems problem={problem} />
        <SheetButtons close={onClose} save={save} ok={body !== null} busy={busy} />
      </Stack>}
  </Sheet>;
}

export default function Organization({ config, phone, again }) {
  const { data, failed, loading } = useRead(readOrganization, {}, again);
  const [shown, setShown] = useKept(data);
  const [sheet, setSheet] = React.useState(false);
  const [said, setSaid] = React.useState('');
  const close = () => setSheet(false);
  const done = (answer) => { close(); setShown(answer); setSaid('Saved.'); };
  const stale = async () => {
    close();
    const result = await readOrganization();
    if (result.ok) setShown(result.data);
    setSaid('Someone else changed the details. Reopen Details.');
  };
  return <Stack spacing={2}>
    {loading && !shown && <LinearProgress />}
    {failed && !shown && <Alert severity="error">The organization could not be loaded.</Alert>}
    {shown && <Summary place={shown} phone={phone} open={() => setSheet(true)} />}
    {sheet && shown && <DetailsSheet key={shown.version} place={shown} csrf={config.csrf} phone={phone} onClose={close}
      onDone={done} onStale={stale} />}
    <Toast said={said} clear={() => setSaid('')} />
  </Stack>;
}
