// SPDX-License-Identifier: AGPL-3.0-only
import React from 'react';
import {
  Alert, Box, Button, FormControlLabel, IconButton, LinearProgress, Link, MenuItem, Stack, Switch, Table, TableBody, TableCell, TableHead,
  TableRow, TextField, Typography,
} from '@mui/material';
import AddRounded from '@mui/icons-material/AddRounded';
import ArrowBackRounded from '@mui/icons-material/ArrowBackRounded';
import CloseRounded from '@mui/icons-material/CloseRounded';
import { Sheet } from '../book/AddSheet';
import { changePriceBook, makePriceBook, readPrices, removePriceBook, removePrices, savePrices } from './api';
import { useCrumb } from './crumbs';
import { Card, OpenCard, OpenRow } from './parts';
import { nameOk } from './setup';
import {
  DAY, LENGTH, PER_BOOKING, PER_SPOT, TIME_OF_DAY, amountOf, bookBody, bookNameTaken, containerNo, daysText, draftsOf, exampleText, goingBy,
  hm, length, maxOf, missing, money, priceFailure, pricedNames, saveBody, sheetRefusals, spotWord, steps, timesText,
  withTier,
} from './prices';
import { Problems, SheetButtons, Toast, useKept } from './setupParts';
import useRead from './useRead';

// The Price books: the list of a book's containers with what is priced and what is not, a price
// page per container with a card per booking category, the entry sheet (the
// normal price and, on a shared container, one for each group of spots with
// its own) and the book sheet. The server (/admin/prices/...) keeps the rules
// and answers the whole place after every change. Which book a container names
// is the container's own field (part 7.96); it is shown here, never changed.

const menu = { select: { MenuProps: { slotProps: { paper: { sx: { maxHeight: 280 } } } } } };
const small = { '& td, & th': { px: 1.5, py: 0.75, whiteSpace: 'nowrap' } };
const tight = { '& td, & th': { px: 1.5, whiteSpace: 'nowrap' } };

// One call, its busy and its failure; `done` gets the new place.
function useRun(onDone) {
  const [busy, setBusy] = React.useState(false);
  const [problem, setProblem] = React.useState(null);
  const run = async (call, toast) => {
    if (busy) return;
    setBusy(true);
    setProblem(null);
    const result = await call();
    if (result.kind === 'signed-out') {
      window.location.assign('/login/');
      return;
    }
    if (result.code === 'changed-meanwhile') {
      // Someone else changed these prices: show them as they are now.
      const fresh = await readPrices();
      if (fresh.ok) {
        setBusy(false);
        onDone(fresh.data, 'Changed by someone else.');
        return;
      }
    }
    setBusy(false);
    if (result.ok) onDone(result.data, toast);
    else setProblem(priceFailure(result));
  };
  return { busy, problem, setProblem, run };
}

function Tiers({ d, c, cur, set, busy }) {
  const time = d.tiers_go_by === TIME_OF_DAY;
  const ups = time ? steps(c.start + 15, DAY) : steps(15, Math.max(maxOf(c), 15));
  const pers = steps(15, Math.max(maxOf(c), 15));
  const put = (i, patch) => set({ ...d, tiers: d.tiers.map((x, j) => (j === i ? { ...x, ...patch } : x)) });
  const select = (name, value, options, show, onChange, width) => <TextField select size="small" label={name} value={value} disabled={busy}
    onChange={(x) => onChange(x.target.value)} slotProps={menu} sx={{ width }}>
    {[...new Set([value, ...options])].sort((a, b) => a - b).map((m) => <MenuItem key={m} value={m}>{show(m)}</MenuItem>)}
  </TextField>;
  return <Stack spacing={1.75}>
    {d.tiers.map((x, i) => <Stack key={i} direction="row" spacing={0.5} sx={{ alignItems: 'center', flexWrap: 'nowrap' }}>
      {select('Up to', x.up_to, ups, time ? hm : length, (v) => put(i, { up_to: v }), 88)}
      {select('Paid per', x.paid_per, pers, length, (v) => put(i, { paid_per: v }), 84)}
      <TextField size="small" label="Amount" value={x.text} disabled={busy} error={Number.isNaN(amountOf(x.text))}
        onChange={(v) => put(i, { text: v.target.value })}
        slotProps={{ inputLabel: { shrink: true }, htmlInput: { inputMode: 'decimal' }, input: { endAdornment: <Typography variant="body2" sx={{ color: 'text.secondary', ml: 1 }}>{cur}</Typography> } }}
        sx={{ width: 108 }} />
      {d.tiers.length > 1 && <IconButton size="small" aria-label="Remove tier" disabled={busy} sx={{ bgcolor: 'transparent', color: 'text.secondary' }}
        onClick={() => set({ ...d, tiers: d.tiers.filter((_, j) => j !== i) })}>
        <CloseRounded fontSize="small" /></IconButton>}
    </Stack>)}
    <Button size="small" startIcon={<AddRounded />} sx={{ alignSelf: 'flex-start' }} disabled={busy} onClick={() => set(withTier(d, c))}>Tier</Button>
  </Stack>;
}

// One container's price in one category: its normal entry and, on a shared
// container, one for each group of spots that has its own.
function EntrySheet({ c, category, book, csrf, phone, onClose, onDone }) {
  const word = spotWord(c);
  const [drafts, setDrafts] = React.useState(() => draftsOf(c, category.id));
  const [which, setWhich] = React.useState('normal');
  const [refused, setRefused] = React.useState([]);
  const { busy, problem, setProblem, run } = useRun(onDone);
  const put = (key, value) => { setDrafts({ ...drafts, [key]: value }); setRefused([]); setProblem(null); };
  const d = drafts[which];
  const save = () => {
    if (busy) return;
    const found = sheetRefusals(drafts, c, word);
    if (found.length) { setRefused(found); return; }
    run(() => savePrices(csrf, c.id, category.id, saveBody(drafts, c.groups, c.stamps[category.id])), 'Saved.');
  };
  const remove = drafts.existed && <Button color="error" disabled={busy}
    onClick={() => run(() => removePrices(csrf, c.id, category.id, c.stamps[category.id]), 'Removed.')}>Remove</Button>;
  return <Sheet wide={!phone} onClose={busy ? () => {} : onClose} title={<Box>
    <Typography variant="h6" sx={{ fontWeight: 600 }}>{containerNo(c.number)} {c.type} · {category.name}</Typography>
    <Typography variant="body2" sx={{ color: 'text.secondary' }}>{daysText(c.weekdays)} · {timesText(c)}</Typography>
  </Box>}>
    <Stack spacing={2} component="form" onSubmit={(e) => { e.preventDefault(); save(); }}>
      {c.groups.length > 0 && <TextField select size="small" label="Price for" value={which} disabled={busy} onChange={(v) => setWhich(v.target.value)}>
        <MenuItem value="normal">Normal price</MenuItem>
        {c.groups.map((g) => <MenuItem key={g.id} value={g.id}>{g.name}</MenuItem>)}
      </TextField>}
      {which !== 'normal' && <FormControlLabel label="Own price" control={<Switch checked={!!d} disabled={busy}
        onChange={(v) => put(which, v.target.checked
          ? { ...drafts.normal, group: which, billing_basis: PER_SPOT, tiers: drafts.normal.tiers.map((t) => ({ ...t })) } : null)} />} />}
      {d && <>
        <Stack direction="row" spacing={1}>
          <TextField select size="small" label="Goes by" value={d.tiers_go_by} disabled={busy} onChange={(v) => put(which, goingBy(d, v.target.value, c))} sx={{ width: 150 }}>
            <MenuItem value={LENGTH}>Length</MenuItem>
            <MenuItem value={TIME_OF_DAY} disabled={c.end > 1440}>Time of day</MenuItem>
          </TextField>
          {c.mode === 'shared' && which === 'normal' && <TextField select size="small" label="Paid" value={d.billing_basis} disabled={busy}
            onChange={(v) => put(which, { ...d, billing_basis: v.target.value })} sx={{ width: 150 }}>
            <MenuItem value={PER_BOOKING}>Per booking</MenuItem>
            <MenuItem value={PER_SPOT}>Per {word}</MenuItem>
          </TextField>}
        </Stack>
        <Tiers d={d} c={c} cur={book.currency} set={(v) => put(which, v)} busy={busy} />
      </>}
      <Problems refused={refused} problem={problem} />
      <SheetButtons close={onClose} save={save} busy={busy} left={remove} />
    </Stack>
  </Sheet>;
}

function BookSheet({ book, books, currencies, fallback, csrf, phone, onClose, onDone }) {
  const [name, setName] = React.useState(book?.name ?? '');
  const [currency, setCurrency] = React.useState(book?.currency ?? fallback);
  const { busy, problem, setProblem, run } = useRun((data, toast) => onDone(data, toast, name.trim()));
  const priced = !!book && book.entries > 0;
  const taken = bookNameTaken(books, name, book?.id ?? null);
  const body = bookBody(book, name, currency);
  const ok = nameOk(name) && !taken && (book === null || Object.keys(body).length > 0);
  const save = () => {
    if (!ok || busy) return;
    run(() => (book === null ? makePriceBook(csrf, body) : changePriceBook(csrf, book.id, body)), book === null ? 'Created.' : 'Saved.');
  };
  const remove = book && <Button color="error" disabled={!book.removable || busy}
    onClick={() => run(() => removePriceBook(csrf, book.id), 'Removed.')}>Remove</Button>;
  return <Sheet wide={!phone} onClose={busy ? () => {} : onClose}
    title={<Typography variant="h6" sx={{ fontWeight: 600 }}>{book ? book.name : 'New price book'}</Typography>}>
    <Stack spacing={2} component="form" onSubmit={(e) => { e.preventDefault(); save(); }}>
      <TextField size="small" label="Name" value={name} disabled={busy} onChange={(e) => { setName(e.target.value); setProblem(null); }}
        error={taken} helperText={taken ? 'Taken' : ' '} slotProps={{ htmlInput: { maxLength: 200 } }} />
      <TextField select size="small" label="Currency" value={currency} disabled={priced || busy} onChange={(e) => setCurrency(e.target.value)}>
        {currencies.map((x) => <MenuItem key={x} value={x}>{x}</MenuItem>)}
      </TextField>
      <Problems problem={problem} />
      <SheetButtons close={onClose} save={save} ok={ok} busy={busy} left={remove} />
    </Stack>
  </Sheet>;
}

function TierTable({ e, word, cur }) {
  return <Table size="small" sx={small}>
    <TableHead><TableRow>
      <TableCell>Up to</TableCell><TableCell>Paid per</TableCell><TableCell align="right">Amount</TableCell>
    </TableRow></TableHead>
    <TableBody>
      {e.tiers.map((x, i) => <TableRow key={i}>
        <TableCell>{e.tiers_go_by === LENGTH ? length(x.up_to) : hm(x.up_to)}</TableCell>
        <TableCell>{length(x.paid_per)}{e.billing_basis === PER_SPOT ? ` per ${word}` : ''}</TableCell>
        <TableCell align="right">{money(x.amount, cur)}</TableCell>
      </TableRow>)}
    </TableBody>
  </Table>;
}

function Examples({ e, c, cur }) {
  return <Table size="small" sx={small}>
    <TableHead><TableRow><TableCell>Booking</TableCell><TableCell align="right">Price</TableCell></TableRow></TableHead>
    <TableBody>
      {e.examples.map((g) => <TableRow key={`${g.start}-${g.length}`}>
        <TableCell>{exampleText(e, c, g)}</TableCell><TableCell align="right">{money(g.total, cur)}</TableCell>
      </TableRow>)}
    </TableBody>
  </Table>;
}

// One container's prices in a book: a card per booking category, its tiers
// beside a few bookings and their price; a group's own price below.
function PricePage({ c, book, categories, csrf, back, phone, onDone, toContainer }) {
  const [sheet, setSheet] = React.useState(null);
  const word = spotWord(c);
  const entry = (x, g = null) => c.entries.find((e) => e.category === x && e.group === g);
  const block = (e, title) => <Box sx={{ display: 'grid', gap: 2, gridTemplateColumns: phone ? '1fr' : '1fr 1fr', alignItems: 'start' }}>
    <Box>
      {title && <Typography variant="body2" sx={{ fontWeight: 600, px: 2, pt: 1 }}>{title}</Typography>}
      <TierTable e={e} word={word} cur={book.currency} />
    </Box>
    <Examples e={e} c={c} cur={book.currency} />
  </Box>;
  return <Stack spacing={2}>
    {phone && <Button startIcon={<ArrowBackRounded />} onClick={back} sx={{ alignSelf: 'flex-start' }}>Price books</Button>}
    <Box>
      <Typography component="h1" variant="h5" sx={{ fontWeight: 600 }}>{containerNo(c.number)} {c.type}</Typography>
      <Typography variant="body2" sx={{ color: 'text.secondary' }}>
        {c.asset} · {daysText(c.weekdays)} · {timesText(c)}
        {c.period === 'flexible' && ` · ${length(c.minimum)} to ${length(c.maximum)}`}
        {' · '}<Link component="button" variant="body2" underline="hover" onClick={() => toContainer(c.id)}>Container</Link>
      </Typography>
    </Box>
    {categories.filter((x) => c.categories.includes(x.id)).map((x) => {
      const e = entry(x.id);
      const grouped = c.groups.filter((g) => entry(x.id, g.id));
      return <Card key={x.id} flush title={<>{x.name}{e && <Typography component="span" variant="body2" sx={{ color: 'text.secondary', ml: 1 }}>
        {e.tiers_go_by === LENGTH ? 'by length' : 'by time of day'}</Typography>}</>}
        action={<Button size="small" onClick={() => setSheet(x)}>{e ? 'Edit' : 'Add'}</Button>}>
        {e ? <Box sx={{ pb: 1 }}>
          {block(e, grouped.length ? 'Normal price' : '')}
          {grouped.map((g) => <Box key={g.id} sx={{ mt: 1.5 }}>{block(entry(x.id, g.id), g.name)}</Box>)}
        </Box> : <Typography variant="body2" sx={{ color: 'warning.light', px: 2, pb: 2 }}>No price</Typography>}
      </Card>;
    })}
    {sheet && <EntrySheet key={sheet.id} c={c} category={sheet} book={book} csrf={csrf} phone={phone}
      onClose={() => setSheet(null)} onDone={(data, toast) => { setSheet(null); onDone(data, toast); }} />}
  </Stack>;
}

export default function PricePages({ config, phone, again, toContainer, setCrumb }) {
  const { data, failed, loading } = useRead(readPrices, {}, again);
  const [shown, setShown] = useKept(data);
  const [book, setBook] = React.useState(null);
  const [openId, setOpenId] = React.useState(null);
  const [sheet, setSheet] = React.useState(null);
  const [said, setSaid] = React.useState('');
  const books = shown?.price_books ?? [];
  const containers = shown?.containers ?? [];
  const categories = shown?.booking_categories ?? [];
  const current = books.find((b) => b.id === book) ?? books[0];
  const rows = current ? containers.filter((c) => c.book === current.id) : [];
  const open = openId !== null && rows.find((c) => c.id === openId);
  const onDone = (answer, toast) => { setShown(answer); setSaid(toast); };
  useCrumb(setCrumb, open ? [`${containerNo(open.number)} ${open.type}`] : [], () => setOpenId(null));
  if (open) {
    return <>
      <PricePage c={open} book={current} categories={categories} csrf={config.csrf} back={() => setOpenId(null)} phone={phone} onDone={onDone} toContainer={toContainer} />
      <Toast said={said} clear={() => setSaid('')} />
    </>;
  }
  return <Stack spacing={2}>
    {loading && !shown && <LinearProgress />}
    {failed && !shown && <Alert severity="error">The price books could not be loaded.</Alert>}
    {shown && <>
      <Stack direction="row" spacing={1} sx={{ alignItems: 'center' }}>
        {current && <TextField select size="small" label="Price book" value={current.id} onChange={(e) => setBook(e.target.value)}
          sx={{ width: phone ? 'auto' : 200, flex: phone ? 1 : 'none' }}>
          {books.map((b) => <MenuItem key={b.id} value={b.id}>{b.name} · {b.currency}</MenuItem>)}
        </TextField>}
        {current && <Button onClick={() => setSheet({ book: current })}>Edit</Button>}
        {!phone && <Box sx={{ flex: 1 }} />}
        {phone && !current && <Box sx={{ flex: 1 }} />}
        <Button variant="contained" startIcon={<AddRounded />} onClick={() => setSheet({ book: null })} sx={{ flexShrink: 0 }}>
          {phone ? 'New' : 'New price book'}</Button>
      </Stack>
      {current && <Card flush title={`Containers (${rows.length})`}>
        {!rows.length ? <Typography variant="body2" sx={{ color: 'text.secondary', px: 2, pb: 2 }}>None</Typography>
          : phone ? rows.map((c) => <OpenCard flat key={c.id} open={() => setOpenId(c.id)}>
            <Typography variant="body2" sx={{ fontWeight: 600 }}>{containerNo(c.number)} {c.type}</Typography>
            <Typography variant="caption" component="div" sx={{ color: 'text.secondary' }}>{daysText(c.weekdays)} · {timesText(c)}</Typography>
            <Typography variant="caption" component="div" sx={{ color: missing(c, categories).length ? 'warning.light' : 'text.secondary' }}>
              {missing(c, categories).length ? `No price: ${missing(c, categories).join(', ')}` : pricedNames(c, categories)}</Typography>
          </OpenCard>) : <Table size="small" sx={tight}>
            <TableHead><TableRow>
              <TableCell>Container</TableCell><TableCell>Booking type</TableCell><TableCell>Starts on</TableCell><TableCell>Times</TableCell>
              <TableCell>Priced</TableCell><TableCell>No price</TableCell>
            </TableRow></TableHead>
            <TableBody>
              {rows.map((c) => <OpenRow key={c.id} open={() => setOpenId(c.id)}>
                <TableCell sx={{ fontWeight: 600 }}>{containerNo(c.number)}</TableCell><TableCell>{c.type}</TableCell>
                <TableCell>{daysText(c.weekdays)}</TableCell><TableCell>{timesText(c)}</TableCell>
                <TableCell>{pricedNames(c, categories)}</TableCell>
                <TableCell sx={{ color: missing(c, categories).length ? 'warning.light' : 'text.secondary' }}>{missing(c, categories).join(', ') || '–'}</TableCell>
              </OpenRow>)}
            </TableBody>
          </Table>}
      </Card>}
    </>}
    {sheet && <BookSheet key={sheet.book?.id ?? 'new'} book={sheet.book} books={books} currencies={shown.currencies} fallback={shown.currency}
      csrf={config.csrf} phone={phone} onClose={() => setSheet(null)}
      onDone={(answer, toast, name) => {
        setSheet(null);
        onDone(answer, toast);
        // A new book is shown; after a removal the first one is.
        if (toast === 'Created.') setBook(answer.price_books.find((b) => b.name === name)?.id ?? null);
        if (toast === 'Removed.') setBook(null);
      }} />}
    <Toast said={said} clear={() => setSaid('')} />
  </Stack>;
}
