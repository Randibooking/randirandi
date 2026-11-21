// SPDX-License-Identifier: AGPL-3.0-only
import React from 'react';
import {
  Alert, Box, Button, Divider, LinearProgress, MenuItem, Snackbar, Stack, Tab, Table, TableBody, TableCell, TableHead,
  TablePagination, TableRow, TableSortLabel, Tabs, TextField, Typography, Chip, CircularProgress,
} from '@mui/material';
import ArrowBackRounded from '@mui/icons-material/ArrowBackRounded';
import AddRounded from '@mui/icons-material/AddRounded';
import ChevronRightRounded from '@mui/icons-material/ChevronRightRounded';
import { Sheet } from '../book/AddSheet';
import { red } from '../theme/themePrimitives';
import {
  addMember, blockMember, changeMemberEmail, correctMember, exportMemberData, readBookings, readMember, readMembers, resetMemberAuthenticator, unblockMember,
} from './api';
import { IMPORTS_ADDRESS, figure } from './bookings';
import { ImportTab } from './ImportPages';
import { hrefOf } from './places';
import { plainClick } from './route';
import {
  MEMBER_STATUS_COLORS, addBody, addReady, addTitle, addressWords, blockFailure, blockTitle, blockToast, changeFailure, codeBody, correctBody, correctReady, correctTitle,
  emailBody, emailReady, emailTitle, exportFileName, exportTitle, firstDirection, lastLoginWords, memberBookingsQuery, membersQuery,
  canResetAuthenticator, originWords, phoneWords, postalWords, resetTitle, sortedBy, startRole,
} from './members';
import { Card, Field, MemberSearch, OpenCard, OpenRow, Progress, StateChip, boxStyle } from './parts';
import useRead from './useRead';

// The Members, the Members tab only. The list is the server's
// answer in pages (/admin/members/list/); a row opens the member's page, whose
// words and numbers are the server's answer for /admin/members/<id>/data/.
// Correct, Change email and Export data are sheets over the page (part 7.92),
// Reset authenticator one more (part 7.993); Add member is a sheet over the
// list (part 8.2). The Imports tab (part 7.93) is ImportPages.jsx;
// MembersPlace holds the two tabs.

function MemberChip({ status, label }) {
  return <Chip size="small" variant="outlined" label={label} color={MEMBER_STATUS_COLORS[status] ?? 'default'} />;
}

// The Members place: its two tabs, as the drawing has them. Each tab is a link
// to its own address; each stays drawn once shown, for its filters and page.
export function MembersPlace({ config, phone, again, open, tab, openTab, openRun, onStarted }) {
  const seen = React.useRef({});
  seen.current[tab] = true;
  const link = (value, label, href) => <Tab value={value} label={label} component="a" href={href}
    onClick={(event) => { if (plainClick(event)) { event.preventDefault(); openTab(value); } }} />;
  return <Stack spacing={2}>
    <Tabs value={tab} sx={{ borderBottom: 1, borderColor: 'divider', minHeight: 40, '& .MuiTab-root': { minHeight: 40 } }}>
      {link('members', 'Members', hrefOf('members'))}
      {link('imports', 'Imports', IMPORTS_ADDRESS)}
    </Tabs>
    {seen.current.members && <Box sx={{ display: tab === 'members' ? 'block' : 'none' }}>
      <MemberList config={config} phone={phone} open={open} again={again} /></Box>}
    {seen.current.imports && <Box sx={{ display: tab === 'imports' ? 'block' : 'none' }}>
      <ImportTab phone={phone} again={again} csrf={config.csrf} openRun={openRun} onStarted={onStarted} /></Box>}
  </Stack>;
}


// The sheet over the list that adds a member, as the Correct sheet is made: the
// fields, the authenticator code, Cancel and the action. The role starts on the
// organization's standard role. The server judges the code and every value; a
// refusal is shown above the buttons and the code is emptied. The new member's
// page opens after the add.
function AddMemberSheet({ roles, standard, phone, csrf, onClose, onDone }) {
  const [name, setName] = React.useState('');
  const [email, setEmail] = React.useState('');
  const [number, setNumber] = React.useState('');
  const [role, setRole] = React.useState(standard);
  const [code, setCode] = React.useState('');
  const [busy, setBusy] = React.useState(false);
  const [problem, setProblem] = React.useState(null);
  const fields = { name, email, phone: number, role, code };
  const ready = addReady(fields);
  const send = async () => {
    if (busy || !ready) return;
    setBusy(true);
    setProblem(null);
    const result = await addMember(csrf, addBody(fields));
    if (result.kind === 'signed-out') {
      window.location.assign('/login/');
      return;
    }
    setBusy(false);
    if (result.ok) onDone(result.data.id);
    else {
      setCode('');
      setProblem(changeFailure(result));
    }
  };
  return <Sheet wide={!phone} title={<Typography variant="h6" sx={{ fontWeight: 600 }}>{addTitle}</Typography>}
    onClose={busy ? () => {} : onClose}>
    <Stack spacing={2} component="form" onSubmit={(e) => { e.preventDefault(); send(); }}>
      <TextField size="small" label="Full name" value={name} onChange={(e) => setName(e.target.value)} disabled={busy} autoComplete="off" />
      <TextField size="small" type="email" label="Email" value={email} onChange={(e) => setEmail(e.target.value)} disabled={busy} autoComplete="off" />
      <TextField size="small" label="Phone (optional)" placeholder="+47 912 34 567" value={number}
        onChange={(e) => setNumber(e.target.value)} disabled={busy} autoComplete="off" />
      <TextField select size="small" label="Role" value={role} onChange={(e) => setRole(e.target.value)} disabled={busy}>
        {roles.map((r) => <MenuItem key={r.value} value={r.value}>{r.label}</MenuItem>)}
      </TextField>
      <TextField size="small" fullWidth label="Code from your authenticator app" placeholder="123 456" value={code} disabled={busy}
        onChange={(e) => setCode(e.target.value)} autoComplete="one-time-code" slotProps={{ htmlInput: { inputMode: 'numeric' } }} />
      {problem && <Alert severity="error">{problem}</Alert>}
      <Stack direction="row" spacing={1} sx={{ justifyContent: 'flex-end' }}>
        <Button onClick={onClose} disabled={busy}>Cancel</Button>
        <Button type="submit" variant="contained" disabled={busy || !ready}
          startIcon={busy ? <CircularProgress size={16} color="inherit" /> : null}>Add</Button>
      </Stack>
    </Stack>
  </Sheet>;
}

export function MemberList({ config, phone, open, again }) {
  const [adding, setAdding] = React.useState(false);
  const [filters, setFilters] = React.useState({ status: 'all', role: 'all', q: '', sort: 'name', dir: 'asc', page: 0 });
  // The filters change the page; a new filter starts on the first.
  const change = (patch) => setFilters((f) => ({ ...f, page: 0, ...patch }));
  const { data, failed, loading } = useRead(readMembers, membersQuery(filters), again);
  const rows = data?.members ?? [];
  const head = (key, text) => <TableCell sortDirection={filters.sort === key ? filters.dir : false}>
    <TableSortLabel active={filters.sort === key} direction={filters.sort === key ? filters.dir : firstDirection(key)}
      onClick={() => change(sortedBy(filters, key))}>{text}</TableSortLabel>
  </TableCell>;
  const none = <Typography sx={{ color: 'text.secondary', py: phone ? 2 : 4, textAlign: 'center' }}>No member matches.</Typography>;
  const addButton = <Button variant="contained" startIcon={<AddRounded />} onClick={() => setAdding(true)} disabled={!data}
    fullWidth={phone} sx={phone ? undefined : { ml: 'auto' }}>Add member</Button>;
  return <Stack spacing={2}>
    {phone && addButton}
    <Stack direction="row" sx={{ flexWrap: 'wrap', gap: 1.5 }}>
      <TextField select size="small" label="Status" value={filters.status} onChange={(e) => change({ status: e.target.value })} sx={{ minWidth: 140 }}>
        <MenuItem value="all">Every status</MenuItem>
        {(data?.statuses ?? []).map((s) => <MenuItem key={s.value} value={s.value}>{s.label}</MenuItem>)}
      </TextField>
      <TextField select size="small" label="Role" value={filters.role} onChange={(e) => change({ role: e.target.value })} sx={{ minWidth: 150 }}>
        <MenuItem value="all">Every role</MenuItem>
        {(data?.roles ?? []).map((r) => <MenuItem key={r.value} value={r.value}>{r.label}</MenuItem>)}
      </TextField>
      <MemberSearch value={filters.q} set={(q) => change({ q })} phone={phone} placeholder="Name, email, phone or membership" widest={340} />
      {!phone && addButton}
    </Stack>
    {failed && <Alert severity="error">The members could not be loaded.</Alert>}
    <Progress loading={loading} />
    {data && (phone ? <Stack spacing={1}>
      {rows.map((m) => <OpenCard key={m.id} open={() => open(m.id)}>
        <Stack direction="row" spacing={1} sx={{ alignItems: 'center' }}>
          <Typography variant="body2" sx={{ fontWeight: 600, flex: 1 }}>{m.name}</Typography>
          <MemberChip status={m.status} label={m.status_label} />
        </Stack>
        <Typography variant="body2" sx={{ color: 'text.secondary' }}>{m.email}</Typography>
        <Typography variant="caption" sx={{ color: 'text.secondary', display: 'block' }}>
          {m.units.join(', ') || '–'} · {m.role} · last login {m.last_login ?? 'never'}</Typography>
      </OpenCard>)}
      {!rows.length && none}
    </Stack> : <Box sx={boxStyle}>
      <Table size="small" sx={{ '& td, & th': { whiteSpace: 'nowrap', px: 1.5 } }}>
        <TableHead><TableRow>
          {head('name', 'Name')}<TableCell>Email</TableCell><TableCell>Phone</TableCell>{head('unit', 'Membership')}
          <TableCell>Role</TableCell><TableCell>Status</TableCell>{head('login', 'Last login')}
        </TableRow></TableHead>
        <TableBody>
          {rows.map((m) => <OpenRow key={m.id} open={() => open(m.id)}>
            <TableCell sx={{ fontWeight: 600 }}>{m.name}</TableCell>
            <TableCell>{m.email}</TableCell>
            <TableCell sx={{ color: m.phone ? 'inherit' : 'text.secondary' }}>{phoneWords(m.phone) || '–'}</TableCell>
            <TableCell>{m.units.length ? m.units.map((u, i) => <Box key={i}>{u}</Box>) : '–'}</TableCell>
            <TableCell>{m.role}</TableCell>
            <TableCell><MemberChip status={m.status} label={m.status_label} /></TableCell>
            <TableCell sx={{ color: 'text.secondary' }}>{lastLoginWords(m.last_login)}</TableCell>
          </OpenRow>)}
          {!rows.length && <TableRow><TableCell colSpan={7}>{none}</TableCell></TableRow>}
        </TableBody>
      </Table>
    </Box>)}
    {data && data.count > data.per_page && <TablePagination component="div" count={data.count} page={filters.page}
      rowsPerPage={data.per_page} rowsPerPageOptions={[data.per_page]}
      onPageChange={(_, page) => setFilters((f) => ({ ...f, page }))} />}
    {adding && data && <AddMemberSheet roles={data.roles} standard={startRole(data)} phone={phone} csrf={config.csrf}
      onClose={() => setAdding(false)} onDone={(id) => { setAdding(false); open(id); }} />}
  </Stack>;
}

// The question for the member open, asked from the shell, which stays when the
// layout switches between a phone and a wide screen: each question is a look-up
// the server writes down, so it is asked once for each member opened, never
// again for a redraw.
export function useMember(id) {
  const [state, setState] = React.useState({ kind: 'loading' });
  React.useEffect(() => {
    if (id === null) return undefined;
    let current = true;
    setState({ kind: 'loading' });
    readMember(id).then((answer) => {
      if (!current) return;
      if (answer.kind === 'signed-out') window.location.assign('/login/');
      else if (answer.ok) setState({ kind: 'found', member: answer.data });
      else if (answer.kind === 'refused' && answer.status === 404) setState({ kind: 'missing' });
      else setState({ kind: 'failed' });
    });
    return () => { current = false; };
  }, [id]);
  // After a Block or an Unblock the server answers with the member again: shown
  // as is, without a second look-up (which would be written down).
  const replace = React.useCallback((member) => setState({ kind: 'found', member }), []);
  return { ...state, replace };
}

// The question before a Block or an Unblock: Cancel and the action. A second
// press while the first is on its way does nothing, and the server refuses a
// repeat that got through.
function BlockSheet({ member, phone, csrf, onClose, onDone }) {
  const blocked = member.status === 'blocked';
  const [busy, setBusy] = React.useState(false);
  const [problem, setProblem] = React.useState(null);
  const send = async () => {
    if (busy) return;
    setBusy(true);
    setProblem(null);
    const result = await (blocked ? unblockMember : blockMember)(csrf, member.id);
    if (result.kind === 'signed-out') {
      window.location.assign('/login/');
      return;
    }
    setBusy(false);
    if (result.ok) onDone(result.data, blockToast(member));
    else setProblem(blockFailure(result));
  };
  return <Sheet wide={!phone} title={<Typography variant="h6" sx={{ fontWeight: 600 }}>{blockTitle(member)}</Typography>}
    onClose={busy ? () => {} : onClose}>
    <Stack spacing={2}>
      {problem && <Alert severity="error">{problem}</Alert>}
      <Stack direction="row" spacing={1} sx={{ justifyContent: 'flex-end' }}>
        <Button onClick={onClose} disabled={busy}>Cancel</Button>
        <Button variant="contained" color={blocked ? 'primary' : 'error'} onClick={send} disabled={busy}
          startIcon={busy ? <CircularProgress size={16} color="inherit" /> : null}>{blocked ? 'Unblock' : 'Block'}</Button>
      </Stack>
    </Stack>
  </Sheet>;
}

// The sheets of Correct, Change email and Export data, as the operator's drawing
// has them: the fields, "Code from your authenticator app", Cancel and the
// action. The code is used once, so it is emptied after a refusal. The server
// judges the code and every value; a refusal is shown above the buttons.
function MemberSheet({ kind, member, phone, csrf, onClose, onDone }) {
  const [name, setName] = React.useState(member.name);
  const [number, setNumber] = React.useState(member.phone);
  const [role, setRole] = React.useState(member.role_id);
  const [email, setEmail] = React.useState(member.email);
  const [code, setCode] = React.useState('');
  const [busy, setBusy] = React.useState(false);
  const [problem, setProblem] = React.useState(null);
  const corrected = correctBody(member, { name, phone: number, role, code });
  const ready = kind === 'correct' ? correctReady(corrected)
    : kind === 'email' ? emailReady(member, email, code) : code.trim() !== '';
  const send = async () => {
    if (busy || !ready) return;
    setBusy(true);
    setProblem(null);
    const result = await (kind === 'correct' ? correctMember(csrf, member.id, corrected)
      : kind === 'email' ? changeMemberEmail(csrf, member.id, emailBody(email, code))
        : kind === 'reset' ? resetMemberAuthenticator(csrf, member.id, codeBody(code))
          : exportMemberData(csrf, member.id, codeBody(code)));
    if (result.kind === 'signed-out') {
      window.location.assign('/login/');
      return;
    }
    setBusy(false);
    if (result.ok) {
      if (kind === 'export') {
        downloadJson(exportFileName(member.id), result.data);
        onDone(null, 'Downloaded.');
      } else onDone(result.data, kind === 'correct' ? 'Saved.' : kind === 'reset' ? 'Authenticator reset.' : 'Email changed.');
    } else {
      setCode('');
      setProblem(changeFailure(result));
    }
  };
  const title = kind === 'correct' ? correctTitle(member) : kind === 'email' ? emailTitle(member)
    : kind === 'reset' ? resetTitle(member) : exportTitle(member);
  const action = kind === 'correct' ? 'Save' : kind === 'email' ? 'Change email' : kind === 'reset' ? 'Reset' : 'Download';
  return <Sheet wide={!phone} title={<Typography variant="h6" sx={{ fontWeight: 600 }}>{title}</Typography>}
    onClose={busy ? () => {} : onClose}>
    <Stack spacing={2} component="form" onSubmit={(e) => { e.preventDefault(); send(); }}>
      {kind === 'correct' && <>
        <TextField size="small" label="Full name" value={name} onChange={(e) => setName(e.target.value)} disabled={busy} />
        <TextField size="small" label="Phone" value={number} onChange={(e) => setNumber(e.target.value)} disabled={busy} />
        <TextField select size="small" label="Role" value={role} onChange={(e) => setRole(e.target.value)} disabled={busy}>
          {member.roles.map((r) => <MenuItem key={r.value} value={r.value}>{r.label}</MenuItem>)}
        </TextField>
      </>}
      {kind === 'email' && <TextField size="small" type="email" label="New email" value={email} disabled={busy}
        onChange={(e) => setEmail(e.target.value)} />}
      <TextField size="small" fullWidth label="Code from your authenticator app" placeholder="123 456" value={code} disabled={busy}
        onChange={(e) => setCode(e.target.value)} autoComplete="one-time-code" slotProps={{ htmlInput: { inputMode: 'numeric' } }} />
      {problem && <Alert severity="error">{problem}</Alert>}
      <Stack direction="row" spacing={1} sx={{ justifyContent: 'flex-end' }}>
        <Button onClick={onClose} disabled={busy}>Cancel</Button>
        <Button type="submit" variant="contained" disabled={busy || !ready}
          startIcon={busy ? <CircularProgress size={16} color="inherit" /> : null}>{action}</Button>
      </Stack>
    </Stack>
  </Sheet>;
}

// The member's data as a file the browser saves.
function downloadJson(name, data) {
  const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' }));
  const link = document.createElement('a');
  link.href = url;
  link.download = name;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

const when = { color: 'text.secondary', width: 170, flexShrink: 0, whiteSpace: 'nowrap' };

// The member's bookings: the Bookings' list asked with the member, newest first.
function MemberBookings({ id, phone, open }) {
  const [page, setPage] = React.useState(0);
  const { data, failed } = useRead(readBookings, memberBookingsQuery(id, page));
  const rows = data?.bookings ?? [];
  return <Card flush title={data ? `Bookings (${data.count})` : 'Bookings'}>
    {failed && <Alert severity="error" sx={{ m: 2, mt: 0 }}>The bookings could not be loaded.</Alert>}
    {data && !rows.length && <Typography variant="body2" sx={{ color: 'text.secondary', px: 2, pb: 2 }}>No bookings.</Typography>}
    {rows.length > 0 && (phone ? <Stack divider={<Divider />}>
      {rows.map((b) => <OpenCard flat key={b.number} open={() => open(b.number)}>
        <Stack direction="row" spacing={1} sx={{ alignItems: 'center' }}>
          <Typography variant="body2" sx={{ fontWeight: 600, flex: 1 }}>Reference {b.reference}</Typography>
          <StateChip state={b.state} label={b.state_label} />
        </Stack>
        <Typography variant="caption" sx={{ color: 'text.secondary' }}>
          {b.categories} · created {b.created} · {figure(b.amount)} {b.currency}</Typography>
      </OpenCard>)}
    </Stack> : <Table size="small">
      <TableHead><TableRow>
        <TableCell>Reference</TableCell><TableCell>Created</TableCell><TableCell>Category</TableCell><TableCell>State</TableCell>
        <TableCell align="right">Amount</TableCell><TableCell>Currency</TableCell><TableCell padding="checkbox" />
      </TableRow></TableHead>
      <TableBody>
        {rows.map((b) => <OpenRow key={b.number} open={() => open(b.number)}>
          <TableCell sx={{ fontWeight: 600 }}>{b.reference}</TableCell>
          <TableCell sx={{ whiteSpace: 'nowrap', color: 'text.secondary' }}>{b.created}</TableCell>
          <TableCell>{b.categories}</TableCell>
          <TableCell><StateChip state={b.state} label={b.state_label} /></TableCell>
          <TableCell align="right">{figure(b.amount)}</TableCell>
          <TableCell sx={{ color: 'text.secondary' }}>{b.currency}</TableCell>
          <TableCell padding="checkbox"><ChevronRightRounded fontSize="small" sx={{ color: 'text.secondary' }} /></TableCell>
        </OpenRow>)}
      </TableBody>
    </Table>)}
    {data && data.count > data.per_page && <TablePagination component="div" count={data.count} page={page}
      rowsPerPage={data.per_page} rowsPerPageOptions={[data.per_page]} onPageChange={(_, next) => setPage(next)} />}
  </Card>;
}

export default function MemberPage({ id, state: asked, phone, back, csrf, openBooking }) {
  const [asking, setAsking] = React.useState(null);
  const [toast, setToast] = React.useState('');
  // An answer for another member is not this one's.
  const state = asked.kind === 'found' && asked.member.id !== id ? { kind: 'loading' } : asked;
  const { member: m } = state;
  const backButton = phone && <Button startIcon={<ArrowBackRounded />} onClick={back} sx={{ alignSelf: 'flex-start' }}>Members</Button>;
  if (state.kind !== 'found') {
    return <Stack spacing={2}>
      {backButton}
      <Typography component="h1" variant="h5" sx={{ fontWeight: 600 }}>Member {id}</Typography>
      {state.kind === 'loading' && <LinearProgress />}
      {state.kind === 'missing' && <Typography sx={{ color: 'text.secondary' }}>None</Typography>}
      {state.kind === 'failed' && <Alert severity="error">The member could not be loaded.</Alert>}
    </Stack>;
  }
  const rowStyle = { direction: phone ? 'column' : 'row', spacing: phone ? 0 : 2 };
  return <Stack spacing={2}>
    {backButton}
    <Stack direction={phone ? 'column' : 'row'} spacing={1.5} sx={{ alignItems: phone ? 'flex-start' : 'center' }}>
      <Box sx={{ flex: 1 }}>
        <Stack direction="row" spacing={1} sx={{ alignItems: 'center' }}>
          <Typography component="h1" variant="h5" sx={{ fontWeight: 600 }}>{m.name}</Typography>
          <MemberChip status={m.status} label={m.status_label} />
        </Stack>
        <Typography sx={{ color: 'text.secondary', mt: 0.5 }}>{originWords(m)}</Typography>
      </Box>
      <Stack direction="row" spacing={1} sx={{ flexWrap: 'wrap', gap: 1 }}>
        <Button variant="outlined" color="inherit" onClick={() => setAsking('export')}>Export data</Button>
        {canResetAuthenticator(m) && <Button variant="outlined" color="inherit" onClick={() => setAsking('reset')}>Reset authenticator</Button>}
        {!m.own && (m.status === 'blocked'
          ? <Button variant="outlined" color="inherit" onClick={() => setAsking('block')}>Unblock</Button>
          : <Button variant="outlined" sx={{ color: red[300], borderColor: red[400] }} onClick={() => setAsking('block')}>Block</Button>)}
      </Stack>
    </Stack>
    <Box sx={{ display: 'grid', gap: 2, gridTemplateColumns: phone ? '1fr' : '1fr 1fr' }}>
      <Card title="Details" action={m.email ? <Stack direction="row" spacing={0.5}>
        <Button size="small" onClick={() => setAsking('correct')}>Correct</Button>
        <Button size="small" onClick={() => setAsking('email')}>Change email</Button>
      </Stack> : null}>
        <Field label="Full name">{m.name}</Field>
        <Field label="First name">{m.first_name || '–'}</Field>
        <Field label="Last name">{m.last_name || '–'}</Field>
        <Field label="Email">{m.email || '–'}</Field>
        <Field label="Phone">{phoneWords(m.phone) || '–'}</Field>
        <Field label="Role">{m.role}</Field>
      </Card>
      <Card title={`Memberships (${m.units.length})`}>
        {m.units.length ? m.units.map((u, i) => <Box key={i}>
          {i > 0 && <Divider sx={{ my: 1 }} />}
          <Field label="Membership">{u.identifier}</Field>
          <Field label="Address">{addressWords(u)}</Field>
          <Field label="Section">{u.section || '–'}</Field>
          <Field label="Postal code">{postalWords(u)}</Field>
        </Box>) : <Typography variant="body2" sx={{ color: 'text.secondary' }}>None</Typography>}
      </Card>
    </Box>
    <Card title="Status history">
      {m.history.length ? <Stack spacing={1}>
        {m.history.map((e, i) => <Stack key={i} {...rowStyle}>
          <Typography variant="body2" sx={when}>{e.at}</Typography>
          <Typography variant="body2" sx={{ width: 90, flexShrink: 0, fontWeight: 600 }}>{e.status_label}</Typography>
          <Typography variant="body2" sx={{ color: 'text.secondary' }}>{e.cause}</Typography>
        </Stack>)}
      </Stack> : <Typography variant="body2" sx={{ color: 'text.secondary' }}>None</Typography>}
    </Card>
    <Card title="Login history">
      {m.logins.length ? <Stack spacing={1}>
        {m.logins.map((e, i) => <Stack key={i} {...rowStyle}>
          <Typography variant="body2" sx={when}>{e.at}</Typography>
          <Stack direction="row" spacing={phone ? 1 : 2}>
            <Typography variant="body2" sx={{ width: phone ? 'auto' : 240, flexShrink: 0 }}>{e.kind_label}{phone && ':'}</Typography>
            <Typography variant="body2" sx={{ color: e.outcome === 'refused' || e.outcome === 'limited' ? 'warning.light' : 'text.secondary' }}>
              {e.outcome_label}</Typography>
          </Stack>
        </Stack>)}
      </Stack> : <Typography variant="body2" sx={{ color: 'text.secondary' }}>None</Typography>}
    </Card>
    <MemberBookings id={m.id} phone={phone} open={openBooking} />
    {asking === 'block' && <BlockSheet member={m} phone={phone} csrf={csrf} onClose={() => setAsking(null)}
      onDone={(next, text) => { setAsking(null); asked.replace(next); setToast(text); }} />}
    {(asking === 'correct' || asking === 'email' || asking === 'export' || asking === 'reset') && <MemberSheet kind={asking} member={m} phone={phone} csrf={csrf}
      onClose={() => setAsking(null)}
      onDone={(next, text) => { setAsking(null); if (next) asked.replace(next); setToast(text); }} />}
    <Snackbar open={!!toast} autoHideDuration={3500} onClose={() => setToast('')} message={toast}
      anchorOrigin={{ vertical: 'bottom', horizontal: 'center' }} />
  </Stack>;
}
