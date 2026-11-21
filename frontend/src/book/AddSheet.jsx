// SPDX-License-Identifier: AGPL-3.0-only
import React from 'react';
import {
  Alert, Box, Button, Chip, CircularProgress, Dialog, Drawer, IconButton, Link, Stack, TextField, ToggleButton, ToggleButtonGroup, Typography,
} from '@mui/material';
import ApartmentRounded from '@mui/icons-material/ApartmentRounded';
import GroupsRounded from '@mui/icons-material/GroupsRounded';
import HomeRounded from '@mui/icons-material/HomeRounded';
import PersonRounded from '@mui/icons-material/PersonRounded';
import WorkRounded from '@mui/icons-material/WorkRounded';
import DayStrip from './DayStrip';
import { categoryIcon } from './categories.js';
import CloseRounded from '@mui/icons-material/CloseRounded';
import BedtimeOutlined from '@mui/icons-material/BedtimeOutlined';
import WbSunnyOutlined from '@mui/icons-material/WbSunnyOutlined';
import { brand, gray, orange } from '../theme/themePrimitives';
import { addToCart, readPrice } from './api';
import {
  afterFailure, bookingBody, endStopWords, endsFor, firstChoice, isNight, lengthWords, moneyWords, moveStart, picksOk, priceQuery,
  priceView, quantityOf, quoteOf, refusedWords, spotsFor, startsOf, submitAdd, togglePick,
} from './add.js';
import {
  estimate, estimateLines, sliderModel, slideTo,
} from './day.js';
import { label } from './labels.js';
import { CART_HREF } from './places.js';
import { dateWords, hhmm, hoursWords, sentence } from './words.js';

// The sheet, the booking form and the price lines; the data is the server's.

const TITLE = 'add-sheet-title';
// The categories' icons, as the page's Booking as switch has them (FilterControls.jsx).
const CATEGORY_ICONS = {
  person: PersonRounded, work: WorkRounded, building: ApartmentRounded, home: HomeRounded, group: GroupsRounded,
};
// Milliseconds a changed choice rests before its price is asked.
const ASK_AFTER = 250;

// Whether Book shows. Book stays mounted behind the other views, and its
// sheets are drawn over the page, so they show only while it does.
export const BookShown = React.createContext(true);

export function Sheet({ wide, title, onClose, children }) {
  const shown = React.useContext(BookShown);
  const head = <Stack direction="row" sx={{ alignItems: 'flex-start', mb: 1.5 }}>
    <Box id={TITLE} sx={{ flex: 1 }}>{title}</Box>
    <IconButton size="small" onClick={onClose} aria-label={label('common.close')}><CloseRounded fontSize="small" /></IconButton>
  </Stack>;
  if (wide) {
    return <Dialog open={shown} onClose={onClose} fullWidth maxWidth="xs" aria-labelledby={TITLE}
      slotProps={{ paper: { sx: { bgcolor: 'background.paper', backgroundImage: 'none' } } }}>
      <Box sx={{ p: 3 }}>{head}{children}</Box>
    </Dialog>;
  }
  // The Drawer's paper is the dialog; its root is only presentation.
  return <Drawer anchor="bottom" open={shown} onClose={onClose}
    slotProps={{ paper: { 'aria-labelledby': TITLE, sx: { bgcolor: 'background.paper', backgroundImage: 'none', borderRadius: '18px 18px 0 0',
      borderTop: 1, borderColor: 'divider', p: 2.5, pb: 'calc(24px + env(safe-area-inset-bottom))', maxHeight: '88%' } } }}>
    <Box sx={{ width: 40, height: 4, borderRadius: 2, bgcolor: gray[600], mx: 'auto', mb: 1.5 }} />
    {head}
    {children}
  </Drawer>;
}

// `waiting`: the server's lines for this choice are on their way; the lines
// are the last ones, dimmed, and the total the calendar's tiers give (`firm`),
// or the last one, dimmed too, when the tiers cannot tell.
function PriceLines({ view, waiting = false, firm = true, length = null, fixed = false }) {
  const rows = (view.rows || view.lines.map((line) => ({ span: line.what, hours: null, note: line.how, total: line.total })))
    .map((row, _, all) => (fixed && all.length === 1 ? { ...row, note: null } : row));
  return <Box sx={{ border: 1, borderColor: 'divider', borderRadius: 2, p: 1.5 }}>
    {rows.map((row, index) => <Stack key={index} direction="row" sx={{ justifyContent: 'space-between', gap: 2, py: 0.5,
      opacity: waiting ? 0.45 : 1, transition: 'opacity 120ms' }}>
      <Box>
        <Typography variant="body2">{row.span}{row.hours && <Box component="span" sx={{ color: 'text.secondary' }}>{` · ${row.hours}`}</Box>}</Typography>
        {row.note && <Typography sx={{ fontSize: 12, color: 'text.secondary' }}>{row.note}</Typography>}
      </Box>
      <Typography variant="body2" sx={{ whiteSpace: 'nowrap' }}>{row.total}</Typography>
    </Stack>)}
    <Stack direction="row" sx={{ justifyContent: 'space-between', opacity: firm ? 1 : 0.45, transition: 'opacity 120ms',
      ...(rows.length > 0 && { borderTop: 1, borderColor: 'divider', mt: 1, pt: 1 }) }}>
      <Typography sx={{ fontWeight: 600 }}>{length ? label('add.total-for', { length }) : label('add.total')}</Typography>
      <Typography sx={{ fontWeight: 600 }}>{view.total ?? label('price.free')}</Typography>
    </Stack>
  </Box>;
}

function Spots({ info, picks, quantity, onPick }) {
  if (!info) return null;
  if (!info.named) {
    return <Stack spacing={0.25}>
      <Typography variant="body2" sx={{ color: 'text.secondary' }}>{info.count}</Typography>
      {info.groupCounts.map((group) => <Typography key={group.id} variant="body2" sx={{ color: 'text.secondary' }}>{group.words}</Typography>)}
    </Stack>;
  }
  const groups = [{ id: null, name: null }, ...info.groups];
  return <Stack spacing={1}>
    <Typography variant="body2" sx={{ color: 'text.secondary' }}>{info.count}</Typography>
    {groups.map((group) => {
      const spots = info.free.filter((spot) => (spot.group ?? null) === group.id);
      if (!spots.length) return null;
      return <Box key={group.id ?? 'none'}>
        {group.name && <Typography variant="caption" sx={{ color: 'text.secondary' }}>{group.name}</Typography>}
        <Stack direction="row" sx={{ flexWrap: 'wrap', gap: 0.75 }}>
          {spots.map((spot) => <Chip key={spot.number} label={spot.name || spot.number}
            aria-pressed={picks.includes(spot.number)}
            color={picks.includes(spot.number) ? 'primary' : 'default'}
            variant={picks.includes(spot.number) ? 'filled' : 'outlined'}
            onClick={() => onPick(togglePick(picks, spot.number, quantity))} />)}
        </Stack>
      </Box>;
    })}
  </Stack>;
}

// `container` is the day's container as model.js built it; `from` the start the
// member tapped, if any.
export default function AddSheet({
  container, date, from, filter, csrf, wide, store, onClose, onAdded, toCart = { href: CART_HREF },
  cart = null, length = null, onCategory = null, toPrices = null,
}) {
  const night = isNight(container);
  const bookingAs = filter?.categories?.find((one) => one.id === filter.category)?.name;
  const starts = React.useMemo(() => startsOf(container), [container]);
  // A changed category opens the dialog again at the time chosen before, its length kept where it fits.
  const [choice, setChoice] = React.useState(() => {
    const first = firstChoice(container, from);
    return first && length ? moveStart(container, first.start, length) || first : first;
  });
  // The time asked for is not open to this category (changed in the dialog).
  const [moved] = React.useState(() => Boolean(from && !startsOf(container).some((one) => one.at === from)));
  const takes = (id) => !container.categories?.length || container.categories.includes(Number(id));
  const [picks, setPicks] = React.useState([]);
  const [price, setPrice] = React.useState({ kind: 'loading' });
  const [again, setAgain] = React.useState(0);
  const [refusal, setRefusal] = React.useState(null);
  const [busy, setBusy] = React.useState(false);
  const quantity = quantityOf(container, filter);
  const spots = choice ? spotsFor(container, choice.start, choice.end) : null;
  const query = choice
    ? priceQuery({ container, filter, start: choice.start.at, end: choice.end, picks })
    : null;
  const asked = JSON.stringify([query, again]);
  // The server's prices for the choices already asked, and the last one shown,
  // so a slide neither waits at every step nor blanks the lines.
  const quotes = React.useRef(new Map());
  const last = React.useRef(null);
  const before = React.useRef(null);

  React.useEffect(() => {
    if (!query || quotes.current.has(asked)) return undefined;
    let live = true;
    // The first choice is asked at once (also when an effect runs twice); a
    // changed one once it rests, not at every step of a slide.
    const delay = before.current === null || before.current === asked ? 0 : ASK_AFTER;
    before.current = asked;
    const wait = setTimeout(() => {
      readPrice(query).then((result) => {
        if (!live) return;
        if (result.kind === 'signed-out') {
          window.location.assign('/login/');
          return;
        }
        const quote = quoteOf(result);
        // Only a free time is kept: a taken one is asked again when it comes back.
        if (quote.kind === 'priced' && quote.price.available !== false) quotes.current.set(asked, quote);
        setPrice({ ...quote, key: asked });
      });
    }, delay);
    return () => { live = false; clearTimeout(wait); };
  }, [asked]);

  if (!choice) return null;

  const ends = endsFor(container, choice.start);
  const move = (start) => {
    const next = moveStart(container, start, (Date.parse(choice.end) - Date.parse(choice.start.at)) / 60000);
    if (!next) return;
    const free = spotsFor(container, next.start, next.end);
    setChoice(next);
    setPicks((was) => was.filter((number) => free?.free.some((spot) => spot.number === number)));
    setRefusal(null);
  };
  const lengthTo = (end) => settle({ start: choice.start, end });
  function settle(next) {
    const free = spotsFor(container, next.start, next.end);
    setChoice(next);
    setPicks((was) => was.filter((number) => free?.free.some((spot) => spot.number === number)));
    setRefusal(null);
  }
  // A flexible container's time is the range slider (the demo's): two thumbs on
  // the starts and ends the server offers, inside the free time that holds the start.
  const slider = sliderModel(container, date, choice);
  const slide = (value, thumb) => {
    const next = slideTo(container, date, choice, value, thumb);
    if (next.start.at !== choice.start.at || next.end !== choice.end) settle(next);
  };
  // A free hour on the day's strip moves the start there, also into another
  // free stretch, which the slider cannot reach ().
  const startAt = (at) => {
    const start = starts.find((row) => row.at === at);
    if (start && start.at !== choice.start.at) move(start);
  };
  const stopWords = endStopWords(container, choice, bookingAs);
  const minutes = (Date.parse(choice.end) - Date.parse(choice.start.at)) / 60000;
  const Icon = night ? BedtimeOutlined : WbSunnyOutlined;
  // The server's answer for this very choice; until it comes, the last lines
  // dimmed, with the total the calendar's tiers give. Only the server's price
  // goes into the cart. A choice asked before is its kept answer at once.
  const kept = quotes.current.get(asked);
  const answer = price.key === asked || !kept ? price : { ...kept, key: asked };
  const current = answer.key === asked;
  const priced = current && answer.kind === 'priced' && answer.price.available !== false;
  if (priced) last.current = { price: answer.price, booking: { start: choice.start.at, end: choice.end } };
  // Also when the sheet opens: the total and the lines at once from the calendar's
  // tiers (`guessed`), the server's answer the check. Where the tiers cannot word
  // the lines (a price per place), the last lines dimmed under the tiers' total.
  const guess = current
    ? null
    : estimate(container, filter.category, quantity, choice.start.at, choice.end, filter.group, picks);
  const guessed = current || guess === null
    ? null
    : estimateLines(container, filter.category, quantity, choice.start.at, choice.end, filter.group, picks);
  const shown = last.current ? priceView(last.current.price, last.current.booking) : null;
  const currency = last.current?.price.currency ?? container.prices?.currency;
  let view;
  if (priced || guess === null) view = shown;
  else if (guessed) view = priceView(guessed, { start: choice.start.at, end: choice.end });
  else view = { lines: shown ? shown.lines : [], rows: shown ? shown.rows : null, amount: guess, total: guess ? moneyWords(guess, currency) : null };
  const add = async () => {
    setBusy(true);
    const done = await submitAdd({ csrf, booking: bookingBody(query, answer.price.total), add: addToCart, store });
    setBusy(false);
    if (done.kind === 'added') onAdded(done.cart);
    else if (done.kind === 'signed-out') window.location.assign('/login/');
    else {
      const failed = afterFailure(done);
      setRefusal(failed);
      if (failed.requote) setAgain((count) => count + 1);
    }
  };
  const endWords = (end) => `${hhmm(end)} · ${lengthWords((Date.parse(end) - Date.parse(choice.start.at)) / 60000)}`;

  const title = <Stack spacing={0.25}>
    <Stack direction="row" spacing={1} sx={{ alignItems: 'center' }}>
      <Icon sx={{ fontSize: 18, color: night ? brand[200] : orange[300] }} />
      <Typography variant="h6" sx={{ fontWeight: 600 }}>{container.type.name}</Typography>
    </Stack>
    <Typography variant="body2" sx={{ color: 'text.secondary' }}>{dateWords(date)}</Typography>
  </Stack>;

  return <Sheet wide={wide} onClose={onClose} title={title}>
    <Stack spacing={2}>
      <Typography variant="h5" sx={{ fontWeight: 600 }}>
        {hoursWords(choice.start.at, choice.end)}
        <Typography component="span" variant="body2" sx={{ color: 'text.secondary', whiteSpace: 'nowrap' }}>
          {` · ${lengthWords(minutes)}`}</Typography>
      </Typography>
      <DayStrip container={container} date={date} choice={choice} onStart={startAt} slider={slider} onSlide={slide} />
      {moved && <Alert severity="info">{label('add.moved', { time: hhmm(from), category: bookingAs })}</Alert>}
      {!slider && starts.length > 1 && <TextField select size="small" label={label('add.start')} value={choice.start.at}
        slotProps={{ select: { native: true } }} onChange={(event) => move(starts.find((row) => row.at === event.target.value))}>
        {starts.map((row) => <option key={row.at} value={row.at}>
          {container.periodType === 'fixed' ? hoursWords(row.at, row.end) : hhmm(row.at)}</option>)}
      </TextField>}
      {container.periodType !== 'fixed' && !slider && ends.length > 1 && <TextField select size="small" label={label('add.length')}
        value={choice.end} slotProps={{ select: { native: true } }} onChange={(event) => lengthTo(event.target.value)}>
        {ends.map((row) => <option key={row.end} value={row.end}>{endWords(row.end)}</option>)}
      </TextField>}
      {stopWords && <Typography variant="caption" sx={{ color: 'text.secondary' }}>{stopWords}</Typography>}
      <Spots info={spots} picks={picks} quantity={quantity} onPick={setPicks} />
      {filter.categories.length > 1 && <Box>
        <Stack direction="row" sx={{ alignItems: 'center', gap: 1, flexWrap: 'wrap' }}>
        <Typography variant="caption" sx={{ color: 'text.secondary' }}>{label('filter.booking-as')}</Typography>
        <ToggleButtonGroup exclusive size="small" value={filter.category} aria-label={label('filter.booking-as')}
          onChange={(_, next) => { if (next != null && next !== filter.category) onCategory?.(next, choice); }}>
          {filter.categories.map((row) => {
            const Icon = CATEGORY_ICONS[categoryIcon(row.icon)];
            return <ToggleButton key={row.id} value={row.id} disabled={!takes(row.id) || !onCategory}
              sx={{ gap: 0.5, px: 1, py: 0.25, fontSize: 12, lineHeight: '18px', fontWeight: 600, textTransform: 'none' }}>
              {Icon && <Icon sx={{ fontSize: 14 }} />}{row.name}</ToggleButton>;
          })}
        </ToggleButtonGroup>
        </Stack>
        {filter.categories.filter((row) => !takes(row.id)).map((row) => <Typography key={row.id} variant="caption" component="p"
          sx={{ color: 'text.secondary', mt: 0.5 }}>{label('add.not-as', { type: container.type.name, category: row.name })}</Typography>)}
      </Box>}
      {filter.categories.length < 2 && bookingAs && <Typography variant="body2" sx={{ color: 'text.secondary' }}>{label('line.booking-as', { category: bookingAs })}</Typography>}
      <Stack spacing={2} aria-live="polite" aria-busy={!current}>
      {!current && !view && <Box sx={{ display: 'flex', justifyContent: 'center' }}><CircularProgress size={24} aria-label={label('common.loading')} /></Box>}
      {view && (priced || !current) && <PriceLines view={view} waiting={!priced && !guessed} firm={priced || guess !== null}
        length={container.periodType === 'fixed' ? null : lengthWords(minutes)} fixed={container.periodType === 'fixed'} />}
      {current && answer.kind === 'refused' && <Alert severity="warning">{sentence(answer.text)}</Alert>}
      {current && answer.kind === 'priced' && answer.price.available === false && <Alert severity="warning">{refusedWords()}</Alert>}
      {current && (answer.kind === 'offline' || answer.kind === 'forbidden') && <Alert severity="error">{refusedWords()}</Alert>}
      {refusal && !(current && answer.kind === 'refused') && <Alert severity="warning"
        action={refusal.held ? <Button color="inherit" size="small" href={toCart.href}
          onClick={(event) => { onClose(); toCart.onClick?.(event); }}>{label('add.to-payment')}</Button> : undefined}>
        {sentence(refusal.text)}</Alert>}
      </Stack>
      {toPrices && <Link href={toPrices.href} onClick={(event) => toPrices.onClick?.(event)} variant="body2"
        sx={{ alignSelf: 'flex-start', color: 'text.primary' }}>{label('add.prices-link')}</Link>}
      <Button variant="contained" size="large" disabled={!priced || busy || !picksOk(picks, quantity)} onClick={add}>
        {view && (priced || guess !== null) ? label('add.add-price', { price: view.total ?? label('price.free') }) : label('add.add')}
      </Button>
    </Stack>
  </Sheet>;
}
