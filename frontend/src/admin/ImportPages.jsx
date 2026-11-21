// SPDX-License-Identifier: AGPL-3.0-only
import React from 'react';
import {
  Alert, Box, Button, Chip, CircularProgress, LinearProgress, Link, MenuItem, Stack, Table, TableBody, TableCell, TableHead,
  TablePagination, TableRow, TextField, Typography,
} from '@mui/material';
import ArrowBackRounded from '@mui/icons-material/ArrowBackRounded';
import { readImport, readImports, startImport } from './api';
import { bookingAddress, importAddress, memberAddress } from './bookings';
import {
  BOOKING_REST, COUNT_WORDS, OUTCOME_COLORS, byWords, importsQuery, noteWords, rowLabel, rowWords, runTitle, startFailure, startedWords,
} from './imports';
import { plainClick } from './route';
import { Card, OpenCard, OpenRow, Progress, boxStyle, openBooking, plainLink } from './parts';
import useRead from './useRead';

// The Members place's Imports tab and a run's page. The list and the page are the server's answers for
// /admin/members/imports/list/ and /admin/members/imports/<number>/data/; a
// run starts with the file sent as it is to /admin/members/imports/start/.

function OutcomeChip({ run }) {
  return <Chip size="small" variant="outlined" label={run.outcome_label} color={OUTCOME_COLORS[run.outcome] ?? 'default'} />;
}

// The "Import" card: the parser, the file, Start the import. A second press
// while the first is on its way does nothing.
function StartCard({ parsers, csrf, onStarted }) {
  const [choice, setChoice] = React.useState('');
  const [file, setFile] = React.useState(null);
  const [round, setRound] = React.useState(0);
  const [busy, setBusy] = React.useState(false);
  const [problem, setProblem] = React.useState(null);
  const parser = parsers.find((p) => p.id === choice) ?? parsers[0];
  const start = async () => {
    if (busy || !file || !parser) return;
    setBusy(true);
    setProblem(null);
    const result = await startImport(csrf, parser.id, file);
    if (result.kind === 'signed-out') {
      window.location.assign('/login/');
      return;
    }
    setBusy(false);
    if (result.ok) {
      setFile(null);
      setRound((n) => n + 1);
      onStarted(result.data);
    } else {
      setProblem(startFailure(result));
    }
  };
  return <Card title="Import">
    <Stack spacing={2} sx={{ maxWidth: 640, mt: 2 }}>
      <TextField select size="small" label="Parser" value={parser?.id ?? ''} disabled={busy}
        onChange={(e) => { setChoice(e.target.value); setFile(null); setRound((n) => n + 1); setProblem(null); }}>
        {parsers.map((p) => <MenuItem key={p.id} value={p.id}>{p.name}</MenuItem>)}
      </TextField>
      <Stack direction="row" spacing={1.5} sx={{ alignItems: 'center' }}>
        <Button variant="outlined" color="inherit" component="label" disabled={busy}>Choose the file
          <input key={round} hidden type="file" accept={parser?.accept}
            onChange={(e) => { setFile(e.target.files?.[0] ?? null); setProblem(null); }} /></Button>
        <Typography variant="body2" sx={{ color: 'text.secondary', minWidth: 0, overflowWrap: 'anywhere' }}>{file?.name ?? 'No file chosen'}</Typography>
      </Stack>
      {problem && <Alert severity="error">{problem}</Alert>}
      <Box>
        <Button variant={file ? 'contained' : 'outlined'} disabled={!file || busy} onClick={start}
          startIcon={busy ? <CircularProgress size={16} color="inherit" /> : null}>Start the import</Button>
      </Box>
    </Stack>
  </Card>;
}

export function ImportTab({ phone, again, csrf, openRun, onStarted }) {
  const [page, setPage] = React.useState(0);
  const { data, failed, loading } = useRead(readImports, importsQuery(page), again);
  const runs = data?.imports ?? [];
  const go = (number) => (event) => {
    if (!plainClick(event)) return;
    event.preventDefault();
    openRun(number);
  };
  return <Stack spacing={2}>
    {data && <StartCard parsers={data.parsers} csrf={csrf} onStarted={onStarted} />}
    {failed && <Alert severity="error">The imports could not be loaded.</Alert>}
    <Progress loading={loading} />
    {data && (phone ? <Stack spacing={1}>
      {runs.map((x) => <OpenCard key={x.number} open={() => openRun(x.number)}>
        <Stack direction="row" spacing={1} sx={{ alignItems: 'center' }}>
          <Typography variant="body2" sx={{ fontWeight: 600, flex: 1 }}>{runTitle(x.number)}</Typography>
          <OutcomeChip run={x} />
        </Stack>
        <Typography variant="body2" sx={{ color: 'text.secondary' }}>{x.started} · {byWords(x.by)}</Typography>
        <Typography variant="caption" sx={{ color: 'text.secondary', overflowWrap: 'anywhere' }}>{x.file || '–'}</Typography>
      </OpenCard>)}
      {!runs.length && <Typography sx={{ color: 'text.secondary', py: 2, textAlign: 'center' }}>No imports.</Typography>}
    </Stack> : <Box sx={boxStyle}>
      <Table size="small" sx={{ '& td, & th': { whiteSpace: 'nowrap', px: 1 }, '& td:first-of-type, & th:first-of-type': { pl: 2 } }}>
        <TableHead><TableRow>
          <TableCell>Run</TableCell><TableCell>Started</TableCell><TableCell>By</TableCell><TableCell>File</TableCell><TableCell>Outcome</TableCell>
          {COUNT_WORDS.map((w) => <TableCell key={w} align="right">{w}</TableCell>)}
        </TableRow></TableHead>
        <TableBody>
          {runs.map((x) => <OpenRow key={x.number} open={() => openRun(x.number)}>
            <TableCell><Link href={importAddress(x.number)} onClick={go(x.number)} underline="none" color="inherit" sx={plainLink}>{x.number}</Link></TableCell>
            <TableCell sx={{ color: 'text.secondary' }}>{x.started}</TableCell>
            <TableCell>{byWords(x.by)}</TableCell>
            <TableCell title={x.file} sx={{ color: 'text.secondary', maxWidth: 200, overflow: 'hidden', textOverflow: 'ellipsis' }}>{x.file || '–'}</TableCell>
            <TableCell><OutcomeChip run={x} /></TableCell>
            {x.counts.map((c, i) => <TableCell key={COUNT_WORDS[i]} align="right" sx={{ color: c ? 'inherit' : 'text.secondary' }}>{c}</TableCell>)}
          </OpenRow>)}
          {!runs.length && <TableRow><TableCell colSpan={10}>
            <Typography sx={{ color: 'text.secondary', py: 4, textAlign: 'center' }}>No imports.</Typography></TableCell></TableRow>}
        </TableBody>
      </Table>
    </Box>)}
    {data && data.count > data.per_page && <TablePagination component="div" count={data.count} page={page}
      rowsPerPage={data.per_page} rowsPerPageOptions={[data.per_page]} onPageChange={(_, next) => setPage(next)} />}
  </Stack>;
}

// The question for the run open, asked from the shell, which stays when the
// layout switches between a phone and a wide screen. Each question is a look-up
// the server writes down, so it is asked once for each run opened; a run just
// started is shown from the answer to its start, without a second look-up.
export function useImportRun(number) {
  const [state, setState] = React.useState({ kind: 'loading' });
  const seeded = React.useRef(null);
  React.useEffect(() => {
    if (number === null) return undefined;
    if (seeded.current === number) {
      seeded.current = null;
      return undefined;
    }
    let current = true;
    setState({ kind: 'loading' });
    readImport(number).then((answer) => {
      if (!current) return;
      if (answer.kind === 'signed-out') window.location.assign('/login/');
      else if (answer.ok) setState({ kind: 'found', run: answer.data });
      else if (answer.kind === 'refused' && answer.status === 404) setState({ kind: 'missing' });
      else setState({ kind: 'failed' });
    });
    return () => { current = false; };
  }, [number]);
  const seed = React.useCallback((run) => {
    seeded.current = run.number;
    setState({ kind: 'found', run });
  }, []);
  return { ...state, seed };
}

function Rows({ title, items }) {
  if (!items?.length) return null;
  return <Card title={title}>
    <Stack spacing={0.75}>{items.map((item) => <Stack key={`${item.row}-${item.code}`} direction={{ xs: 'column', sm: 'row' }} spacing={{ xs: 0, sm: 2 }}>
      <Typography variant="body2" sx={{ width: 70, flexShrink: 0, color: 'text.secondary' }}>{rowLabel(item.row)}</Typography>
      <Typography variant="body2">{rowWords(item.code)}</Typography>
    </Stack>)}</Stack>
  </Card>;
}

export default function ImportPage({ number, state: asked, phone, back, openMember, openBooking: openOrder }) {
  // An answer for another run is not this one's.
  const state = asked.kind === 'found' && asked.run.number !== number ? { kind: 'loading' } : asked;
  const backButton = phone && <Button startIcon={<ArrowBackRounded />} onClick={back} sx={{ alignSelf: 'flex-start' }}>Imports</Button>;
  if (state.kind !== 'found') {
    return <Stack spacing={2}>
      {backButton}
      <Typography component="h1" variant="h5" sx={{ fontWeight: 600 }}>{runTitle(number)}</Typography>
      {state.kind === 'loading' && <LinearProgress />}
      {state.kind === 'missing' && <Typography sx={{ color: 'text.secondary' }}>None</Typography>}
      {state.kind === 'failed' && <Alert severity="error">The import could not be loaded.</Alert>}
    </Stack>;
  }
  const { run } = state;
  const note = noteWords(run);
  const memberLink = (member) => <Link href={memberAddress(member.id)} underline="always" color="inherit"
    onClick={(event) => { if (plainClick(event)) { event.preventDefault(); openMember(member.id); } }}>{member.name}</Link>;
  return <Stack spacing={2}>
    {backButton}
    <Box>
      <Stack direction="row" spacing={1} sx={{ alignItems: 'center' }}>
        <Typography component="h1" variant="h5" sx={{ fontWeight: 600 }}>{runTitle(run.number)}</Typography>
        <OutcomeChip run={run} />
      </Stack>
      <Typography sx={{ color: 'text.secondary', mt: 0.5 }}>{startedWords(run)}</Typography>
    </Box>
    {note && <Alert severity={run.outcome === 'failed' ? 'error' : 'warning'}>{note}</Alert>}
    <Box sx={{ display: 'grid', gap: 1.5, gridTemplateColumns: phone ? 'repeat(3, 1fr)' : 'repeat(5, 1fr)' }}>
      {COUNT_WORDS.map((word, i) => <Box key={word} sx={{ border: 1, borderColor: 'divider', borderRadius: 2, p: 1.5, bgcolor: 'background.paper' }}>
        <Typography variant="caption" sx={{ color: 'text.secondary' }}>{word}</Typography>
        <Typography variant="h6" sx={{ fontWeight: 600 }}>{run.counts[i]}</Typography>
      </Box>)}
    </Box>
    {run.disabled.length > 0 && <Card title={run.disabled_total > run.disabled.length ? `Disabled (${run.disabled_total})` : 'Disabled'}>
      <Stack spacing={0.75}>{run.disabled.map((member) => <Typography key={member.id} variant="body2">{memberLink(member)}
        {member.bookings.map((booking) => <Box key={booking.number} component="span" sx={{ color: 'warning.light' }}> · booking{' '}
          <Link href={bookingAddress(booking.number)} underline="always" color="inherit"
            onClick={openBooking(openOrder, booking.number)}>{booking.reference}</Link>
          {' '}{BOOKING_REST}</Box>)}
      </Typography>)}</Stack>
    </Card>}
    <Rows title="For you to check" items={run.check} />
    <Rows title="Rows skipped" items={run.skipped} />
  </Stack>;
}
