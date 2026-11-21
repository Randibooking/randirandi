// SPDX-License-Identifier: AGPL-3.0-only
import React from 'react';
import {
  Alert, Box, Button, ButtonBase, CircularProgress, IconButton, Stack, Typography,
} from '@mui/material';
import ChevronLeftRounded from '@mui/icons-material/ChevronLeftRounded';
import ChevronRightRounded from '@mui/icons-material/ChevronRightRounded';
import BedtimeOutlined from '@mui/icons-material/BedtimeOutlined';
import BedtimeRounded from '@mui/icons-material/BedtimeRounded';
import WbSunnyOutlined from '@mui/icons-material/WbSunnyOutlined';
import CheckCircleRounded from '@mui/icons-material/CheckCircleRounded';
import EventBusyRounded from '@mui/icons-material/EventBusyRounded';
import ShoppingCartOutlined from '@mui/icons-material/ShoppingCartOutlined';
import EventNoteOutlined from '@mui/icons-material/EventNoteOutlined';
import BlockRounded from '@mui/icons-material/BlockRounded';
import {
  booked, bookedStripes, brand, closed, closedStripes, gray, orange,
} from '../theme/themePrimitives';
import { readCalendar } from './api';
import AddSheet from './AddSheet';
import { buttonWords, canBook } from './add.js';
import { calendarQuery } from './filter.js';
import { label } from './labels.js';
import {
  drawsFromKept, keyOf, neighbours, prefetch, settle,
} from './store.js';
import { calendars } from './kept.js';
import { buildCalendar } from './model.js';
import {
  canGoBack, clampMonth, dateStruck, dayOn, monthOf, monthStart, monthLength, numberedWeeks, readDays, shiftMonth, todayTarget,
} from './month.js';
import { dayLines, legendSeen, lineSpan } from './dayline.js';
import { usableDay } from './week.js';
import {
  cartDays, dateMark, joinHours, nightBlock, offerLine, priceWords, strip,
} from './day.js';
import { quantityOf } from './add.js';
import { dayCards, hourName } from './stack.js';
import { showBooked } from './BookedSheet';
import StackSheet from './StackSheet';
import {
  dateWords, emptyDay, isEmptyDay, monthTitle, tileName, weekdayInitial, weekdayShort,
} from './words.js';

// The tile, its bar and date, the legend, the day's containers and bookings;
// the data is the server's.
const FREE = gray[50];
const YOURS = brand[400];

// Dates in the page's own words; the demo's `dateSx` and `tileSx`.
// A date is never coloured ("dont color the dates. Like NEVER!"). A
// full day is struck only when nothing of the member's is on it .
export const dateSx = ({ full, mine, cart = false }) => ({
  ...(dateStruck({ full, mine, cart }) && { color: gray[500], textDecoration: 'line-through', fontWeight: 400 }),
});

// The day's line (`dayline.js`, T3 and the owner, 2026-11-15): each run of
// hours where it falls on the month's span, available pale, booked hatched,
// an event amber, yours teal, in your cart a teal edge; closed and passed hours
// leave the track empty. Where two runs touch, a 1 px gap parts them, so they
// are told apart by more than colour (design review, 2026-11-16: free and event
// 1.4:1); a run is at least 3 px wide, so one hour shows. It replaces the 25
// October stripe of the free and event shares.
const LINE_BOOKED = { backgroundColor: booked.fill, backgroundImage: 'repeating-linear-gradient(135deg, #b5b2aa 0 2px, transparent 2px 4.5px)' };
const LINE = {
  other: LINE_BOOKED, held: LINE_BOOKED, lost: LINE_BOOKED,
  free: { backgroundColor: '#eeeeec' },
  event: { backgroundColor: '#ffc53d' },
  yours: { backgroundColor: '#12a594' },
  cart: { boxShadow: 'inset 0 0 0 1px #12a594' },
};
// A run the next one starts at the end of loses its last pixel to the track.
const PARTED = { clipPath: 'inset(0 1px 0 0)' };
export function DayLine({ runs, span, sx }) {
  if (!span || !runs.length) return null;
  const width = span.to - span.from;
  const at = (minute) => ((minute - span.from) / width) * 100;
  return <Box aria-hidden sx={{ position: 'relative', height: 7, borderRadius: 4, overflow: 'hidden', backgroundColor: 'rgba(238, 238, 236, 0.10)', ...sx }}>
    {runs.map((run, index) => {
      if (!LINE[run.state]) return null;
      return <Box key={run.from} sx={{ position: 'absolute', top: 0, bottom: 0, left: `${at(run.from).toFixed(2)}%`,
        width: `${(at(run.to) - at(run.from)).toFixed(2)}%`, minWidth: 3, ...LINE[run.state], ...(runs[index + 1]?.from === run.to && PARTED) }} />;
    })}
  </Box>;
}

// A night's mark by the date: a moon, grey for someone else's night, teal for
// yours, a teal outline for one in your cart, amber for an event.
const MOON = {
  other: { Icon: BedtimeRounded, color: '#b5b3ad' },
  yours: { Icon: BedtimeRounded, color: '#0bd8b6' },
  cart: { Icon: BedtimeOutlined, color: '#0bd8b6' },
  event: { Icon: BedtimeRounded, color: '#ffc53d' },
};
export function NightMoon({ mark, sx }) {
  if (!MOON[mark]) return null;
  const { Icon, color } = MOON[mark];
  return <Icon aria-hidden sx={{ fontSize: 13, color, ...sx }} />;
}

// The legend's Closed look, also the look of a closed day: the Month's tile and
// the Week's column, in scheme C's red (`closed`). The colour under the stripes is the
// lighter of the two, so a text over it is measured against the worse.
export const CLOSED_LOOK = {
  background: closedStripes(3, 5), backgroundColor: closed.hatch, border: `1px dashed ${closed.edge}`,
};

// A closed day that is not past: nothing on it can be booked (a day gone by keeps the past look).
export const closedAhead = (summary) => Boolean(summary.closed) && summary.state !== 'past';

// The legend draws what the calendar draws, in its shapes (T3, 2026-11-15):
// the line's bars, the closed day's look and the night's moon; each by its
// label key.
const LEGEND = [
  ['state.free', LINE.free],
  ['state.booked', LINE_BOOKED],
  ['state.event', LINE.event],
  ['state.closed', CLOSED_LOOK],
  ['state.cart', LINE.cart],
  ['legend.yours', LINE.yours],
  ['legend.night', null],
];

// An event, a closed time, a cart time and a booked night are in the legend
// only when one is drawn (`seen`, `dayline.js` `legendSeen`).
const ONLY_SEEN = { 'state.event': 'event', 'state.closed': 'block', 'state.cart': 'cart', 'legend.night': 'night' };
export function Legend({ seen = new Set() }) {
  return <Stack direction="row" sx={{ flexWrap: 'wrap', gap: 1.5, rowGap: 0.75 }}>
    {LEGEND.filter(([key]) => !ONLY_SEEN[key] || seen.has(ONLY_SEEN[key])).map(([key, sx]) => <Stack key={key} direction="row" spacing={0.75} sx={{ alignItems: 'center' }}>
      {sx ? <Box sx={{ width: 18, height: 7, borderRadius: '4px', ...sx }} /> : <NightMoon mark="other" sx={{ fontSize: 15 }} />}
      <Typography variant="caption" sx={{ color: 'text.secondary' }}>{label(key)}</Typography>
    </Stack>)}
  </Stack>;
}

// Someone else's booking, and someone paying for one, hatched grey; the
// member's own brand.400.
export const BLOCKS = {
  other: { Icon: EventBusyRounded, sx: { background: bookedStripes(7, 9), color: '#eeeeec', border: `1px solid #494844` } },
  event: { Icon: EventNoteOutlined, sx: { bgcolor: '#302008', color: '#ffe7b3', border: `1px solid #714f19`, borderLeft: `3px solid #ffc53d` } },
  closed: { Icon: BlockRounded, sx: { background: closedStripes(7, 9), backgroundColor: closed.hatch, color: closed.text, border: `1px dashed ${closed.edge}` } },
  held: { Icon: EventBusyRounded, sx: { background: bookedStripes(7, 9), color: '#eeeeec', border: `1px solid #494844` } },
  yours: { Icon: CheckCircleRounded, sx: { bgcolor: '#084843', color: '#adf0dd', border: `1px solid #12a594`, '& .MuiSvgIcon-root': { color: '#0bd8b6' } } },
  cart: { Icon: ShoppingCartOutlined, sx: { border: `1.5px dashed #12a594`, color: '#eeeeec' } },
  // A cart time someone else booked first: taken for you, not blue (3.99799).
  lost: { Icon: EventBusyRounded, sx: { background: bookedStripes(7, 9), color: gray[100], border: `1.5px dashed ${gray[500]}` } },
};

// An event's block is the way to its page in Events (wireframe note 11).
export const EVENT_LINK = { textDecoration: 'none', cursor: 'pointer', '&:hover': { boxShadow: 'inset 0 0 0 1px #ffc53d' },
  '&:focus-visible': { outline: '2px solid #eeeeec', outlineOffset: '2px' } };

// Another member's booking opens its sheet: the name and the way to ask for contact.
export const BOOKED_BUTTON = { width: '100%', font: 'inherit', textAlign: 'left', cursor: 'pointer', '&:hover': { boxShadow: 'inset 0 0 0 1px #eeeeec' },
  '&:focus-visible': { outline: '2px solid #eeeeec', outlineOffset: '2px' } };

// An asset whose containers stack: its hours as a column, each free, booked or
// blank (the drawing's option B). A tap on a free hour opens the offers.
function AssetCard({ card, date, picked, onPick }) {
  return <Box sx={{ border: 1, borderColor: 'divider', borderRadius: 2, p: 1.5, bgcolor: 'background.paper' }}>
    <Stack direction="row" spacing={1} sx={{ alignItems: 'center', mb: 1 }}>
      <WbSunnyOutlined sx={{ fontSize: 18, color: orange[300] }} />
      <Typography sx={{ fontWeight: 600, flex: 1 }}>{card.asset.name}</Typography>
    </Stack>
    <Box sx={{ display: 'grid', gridTemplateColumns: '44px minmax(0, 1fr)', columnGap: 1, rowGap: 0.5, alignItems: 'center' }}>
      {card.hours.map((hour) => {
        const marked = picked === hour.minute;
        let cell = null;
        if (hour.state === 'free') {
          cell = <ButtonBase component="button" aria-label={hourName(date, card.asset, hour)} aria-pressed={marked}
            onClick={() => onPick(hour.minute)} sx={{ display: 'block', textAlign: 'left', px: 1.25, py: 0.875, borderRadius: 1.5,
              ...(marked ? { bgcolor: FREE, color: gray[900], border: '1px solid transparent' }
                : { background: '#2a2a28', border: `1px solid #494844`, borderLeft: `3px solid #eeeeec`, color: '#eeeeec' }) }}>
            <Typography variant="body2" sx={{ fontWeight: 600 }}>{marked ? label('stack.from', { time: hour.label }) : label('state.free')}</Typography>
          </ButtonBase>;
        } else if (hour.state !== 'blank') {
          const { Icon, sx } = BLOCKS[hour.state];
          const open = hour.booked ? { component: 'button', type: 'button', 'aria-haspopup': 'dialog', 'aria-label': `${hour.word}, ${card.asset.name}, ${hour.label}–${hour.end}`,
            onClick: () => showBooked(hour.booked) } : {};
          cell = <Stack direction="row" {...open} spacing={1} sx={{ alignItems: 'center', px: 1.25, py: 0.875, borderRadius: 1.5, ...sx, ...(hour.booked && BOOKED_BUTTON) }}>
            <Icon sx={{ fontSize: 18 }} />
            <Typography variant="body2" sx={{ fontWeight: 600 }}>{hour.word}</Typography>
          </Stack>;
        } else {
          cell = <Box sx={{ minHeight: 36 }} />;
        }
        return <React.Fragment key={hour.minute}>
          <Typography variant="caption" sx={{ color: 'text.secondary' }}>{hour.label}</Typography>
          {cell}
        </React.Fragment>;
      })}
    </Box>
  </Box>;
}

const hatch = (a, b) => `repeating-linear-gradient(135deg, ${a} 0 4px, ${b} 4px 8px)`;

// A block of the hour strip in the legend's look: free a dark card with the pale
// edge along its bottom (the cell is too narrow for one on its side), booked
// hatched grey, someone paying the same, an event amber, yours blue,
// in your cart dashed, closed blank. Each carries its own text colour.
const CELLS = {
  free: { background: '#2a2a28', border: `1px solid #494844`, borderBottom: `3px solid #eeeeec`, color: '#eeeeec' },
  other: { background: bookedStripes(5, 7), border: `1px solid #494844`, color: '#eeeeec' },
  held: { background: bookedStripes(5, 7), border: `1px solid #494844`, color: '#eeeeec' },
  event: { background: '#302008', border: `1px solid #714f19`, borderBottom: `3px solid #ffc53d`, color: '#ffe7b3' },
  closed: { ...CLOSED_LOOK, color: closed.text, border: `1px dashed ${closed.edge}` },
  yours: { background: '#084843', border: `1px solid #12a594`, color: '#adf0dd' },
  cart: { background: 'transparent', border: `1px dashed #12a594`, color: '#eeeeec' },
  lost: { background: bookedStripes(5, 7), border: `1px dashed ${gray[500]}`, color: '#eeeeec' },
  blank: { background: 'transparent', border: '1px solid transparent' },
};

const BLOCK_HOVER = { '&:hover': { boxShadow: `0 0 0 2px #eeeeec inset` } };
// The word shows whole or not at all: it is measured against its block and
// hidden when it would be cut (the block's accessible name speaks the span).
function BlockWord({ word }) {
  const ref = React.useRef(null);
  React.useLayoutEffect(() => {
    const node = ref.current;
    if (!node) return undefined;
    // The text's own width (a range over it, sub-pixel) against the room it has.
    const fit = () => {
      const range = document.createRange();
      range.selectNodeContents(node);
      node.style.visibility = range.getBoundingClientRect().width > node.getBoundingClientRect().width + 0.01 ? 'hidden' : 'visible';
    };
    fit();
    let live = true;
    document.fonts?.ready.then(() => { if (live) fit(); });
    if (typeof ResizeObserver === 'undefined') return () => { live = false; };
    const watcher = new ResizeObserver(fit);
    watcher.observe(node);
    if (node.parentElement) watcher.observe(node.parentElement);
    return () => { live = false; watcher.disconnect(); };
  }, [word]);
  return <Typography ref={ref} noWrap sx={{ fontSize: 11, fontWeight: 500, color: 'inherit', lineHeight: 1.2, textOverflow: 'clip', minWidth: 0, maxWidth: '100%' }}>{word}</Typography>;
}
const blockWord = (word) => (word ? <BlockWord word={word} /> : null);

// One block of the strip or the night: a free one a button that books, a named
// booking a button that opens its sheet, the rest plain and spoken.
function Segment({ block, height, sx, onPick, spare = false }) {
  const own = {
    minWidth: 0, height, borderRadius: '4px', ...CELLS[block.state], display: 'flex', alignItems: 'center', justifyContent: 'flex-start', px: 0.75, overflow: 'hidden', ...sx,
  };
  if (block.state === 'free') {
    // `spare`: the night's free block repeats its button's action, so it leaves
    // the tab order and the accessibility tree; a pointer click still books.
    return spare
      ? <ButtonBase component="button" tabIndex={-1} aria-hidden onClick={onPick} sx={{ ...own, ...BLOCK_HOVER }} />
      : <ButtonBase component="button" aria-label={block.name} onClick={onPick} sx={{ ...own, ...BLOCK_HOVER }} />;
  }
  if (block.booked) {
    return <ButtonBase component="button" aria-haspopup="dialog" aria-label={block.name} onClick={() => showBooked(block.booked)}
      sx={{ ...own, ...BLOCK_HOVER }}>{blockWord(block.word)}</ButtonBase>;
  }
  return <Box role={block.name ? 'img' : undefined} aria-label={block.name ?? undefined}
    aria-hidden={block.name ? undefined : true} sx={own}>{blockWord(block.word)}</Box>;
}

// The hours of a flexible offer: the hours of one booking joined into one block
// that says its word from two hours, free hours one button each, the hour
// ticks under their hours.
function HourStrip({ cells, offer, date, dense, onPick }) {
  const height = dense ? 22 : 28;
  const columns = { display: 'grid', gridTemplateColumns: `repeat(${cells.length}, minmax(0, 1fr))`, columnGap: '2px' };
  return <Box sx={{ mt: 1 }}>
    <Box sx={columns}>
      {joinHours(cells, offer, date).map((block) => <Segment key={block.key} block={block} height={height}
        sx={{ gridColumn: `${block.start} / span ${block.span}` }} onPick={() => onPick(block.pick)} />)}
    </Box>
    <Box sx={{ ...columns, mt: 0.25 }} aria-hidden>
      {cells.map((cell) => <Typography key={cell.minute} variant="caption"
        sx={{ minWidth: 0, fontSize: 10, color: 'text.secondary' }}>{cell.tick ? cell.label.slice(0, 2) : ''}</Typography>)}
    </Box>
  </Box>;
}

// The night (a fixed container) under its line as the same block: who has it,
// or the free look with no word; the free block books like the button.
function NightBlock({ block, dense, onAdd }) {
  return <Box sx={{ mt: 1 }}><Segment block={block} height={dense ? 22 : 28} sx={{ width: '100%' }} spare={block.free} onPick={onAdd} /></Box>;
}

// The Month's card for one offer (the demo's `DayContainers`): its icon and
// type, its price on the right, the line with its hours and what is free, for a
// flexible offer the hour strip, then its button where something is free.
function OfferCard({ card, date, dense, cart, filter, onAdd }) {
  const { item, container } = card;
  const Icon = item.night ? BedtimeOutlined : WbSunnyOutlined;
  const price = priceWords(container, filter.category, quantityOf(container, filter));
  const cells = strip(container, date, cart, item.name);
  const night = cells ? null : nightBlock(container, date, cart, item.free && canBook(container), item.name);
  const bookable = item.free && canBook(container);
  return <Box sx={{ border: 1, borderColor: 'divider', borderRadius: 2, p: 1.5, bgcolor: 'background.paper' }}>
    <Stack direction="row" spacing={1} sx={{ alignItems: 'center' }}>
      <Icon sx={{ fontSize: 18, color: item.night ? brand[200] : orange[300] }} />
      <Typography sx={{ fontWeight: 600, flex: 1 }}>{item.name}</Typography>
      {price && <Typography variant="body2" sx={{ fontWeight: 600, whiteSpace: 'nowrap' }}>{price}</Typography>}
    </Stack>
    <Typography variant="body2" sx={{ color: 'text.secondary', mt: 0.25 }}>{offerLine(container, item.blocks)}</Typography>
    {cells && cells.length > 0 && <HourStrip cells={cells} offer={item.name} date={date} dense={dense} onPick={(from) => onAdd(container, from)} />}
    {night && <NightBlock block={night} dense={dense} onAdd={() => onAdd(container)} />}
    {bookable && <Button size="small" variant="outlined" sx={{ mt: 1 }} onClick={() => onAdd(container)}>
      {buttonWords(container)}
    </Button>}
  </Box>;
}

// The day's offer cards, the Month's and the Week's alike (T3, 2026-11-15: one
// look for "Choose a time"). `order`: the organization's order of booking types.
export function Containers({
  entry, cart, filter, dense, wide = false, order = [], onChange, onAdd,
}) {
  // A closed day says Closed first, whatever else the answer holds for it (an event, a booking
  // from before the closure); what it holds is still drawn, but nothing on it can be booked.
  const shut = Boolean(entry.summary.closed);
  const empty = shut || isEmptyDay(entry) ? emptyDay(entry, filter) : null;
  const [pick, setPick] = React.useState(null);
  return <Stack spacing={1.25}>
    <Typography component="h2" variant="h6" sx={{ fontWeight: 600 }}>{dateWords(entry.date)}</Typography>
    {empty && <Box sx={{ border: 1, borderColor: 'divider', borderRadius: 2, p: dense ? 1 : 1.5, bgcolor: 'background.paper' }}>
      <Typography variant="body2">{empty.line}</Typography>
      {empty.button && <Button size="small" variant="outlined" sx={{ mt: 1 }}
        onClick={() => onChange(empty.button.patch)}>{empty.button.words}</Button>}
    </Box>}
    {(!empty || shut) && dayCards(entry, cart, order).map((card) => {
      if (card.kind === 'asset') {
        return <AssetCard key={card.key} card={card} date={entry.date}
          picked={pick && pick.date === entry.date && pick.asset.id === card.asset.id ? pick.minute : null}
          onPick={(minute) => setPick({ asset: card.asset, containers: card.containers, date: entry.date, minute })} />;
      }
      return <OfferCard key={card.key} card={card} date={entry.date} dense={dense} cart={cart} filter={filter} onAdd={onAdd} />;
    })}
    {pick && pick.date === entry.date && <StackSheet wide={wide} asset={pick.asset} containers={pick.containers} date={entry.date} minute={pick.minute}
      filter={filter} onClose={() => setPick(null)}
      onPick={(container, from) => { setPick(null); onAdd(container, from); }} />}
  </Stack>;
}

// The browser's date, only to pick the first month before any answer has
// said what day the server counts as today.
export const browserToday = () => {
  const now = new Date();
  return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}-${String(now.getDate()).padStart(2, '0')}`;
};

// The month's own days from the server. An answer is kept by its whole
// question (store.js), so a month already seen shows at once and is asked again
// quietly; the months either side are fetched quietly once this one shows.
// `refresh` asks again after a return to Book. A failed quiet read keeps the
// kept answer. What is returned always belongs to this question: never another
// period's data under this period's title.
export function useMonthCalendar(query, month, category, refresh) {
  const [attempt, setAttempt] = React.useState(0);
  // Asked again with the same question (after an add): the old answer stays on
  // screen until the new one is in.
  const [again, setAgain] = React.useState(0);
  const [store] = React.useState(() => calendars);
  const before = React.useRef(month);
  // The first run since the view appeared, and the token of the asks that force
  // a fresh answer (a return to Book, Try again, an add).
  const opening = React.useRef(true);
  const forced = React.useRef(JSON.stringify([refresh, again, attempt]));
  const [state, setState] = React.useState({ kind: 'loading' });
  const asked = JSON.stringify([query, month, category, attempt]);
  const key = keyOf(query, monthStart(month), monthLength(month));
  React.useEffect(() => {
    let live = true;
    const direction = month < before.current ? -1 : 1;
    before.current = month;
    const token = JSON.stringify([refresh, again, attempt]);
    const force = forced.current !== token;
    forced.current = token;
    const first = opening.current;
    opening.current = false;
    const build = (name) => (data) => buildCalendar(data, { start: monthStart(name), days: monthLength(name), category });
    const keyFor = (name) => keyOf(query, monthStart(name), monthLength(name));
    // One ask at a time for a month, whoever wants it (the Week may be asking it too).
    const read = (name) => store.ask(keyFor(name), () => readDays(readCalendar, query, monthStart(name), monthLength(name)));
    const quietly = (built) => prefetch({
      names: neighbours(month, direction, shiftMonth, canGoBack(month, built.today)),
      has: (name) => store.has(keyFor(name)),
      fetch: (name) => read(name).then((quiet) => {
        settle(store, keyFor(name), quiet, build(name));
      }),
      still: () => live,
      wait: (ms) => new Promise((done) => { setTimeout(done, ms); }),
    });
    // A switch from the Week to a month the page holds asks nothing: it draws it.
    const held = store.get(key);
    if (drawsFromKept({ opening: first, forced: force, held: Boolean(held) })) {
      quietly(held);
      return () => { live = false; };
    }
    read(month).then((result) => {
      if (result.kind === 'signed-out') {
        if (live) window.location.assign('/login/');
        return;
      }
      const built = settle(store, key, result, build(month));
      if (live) setState(built ? { asked, kind: 'ready', built } : { asked, kind: 'error' });
      if (!built) return;
      quietly(built);
    });
    return () => { live = false; };
  }, [asked, refresh, again]);
  const kept = store.get(key);
  let shown;
  if (kept) shown = { asked, kind: 'ready', built: kept };
  else if (state.asked === asked) shown = state;
  else shown = { kind: 'loading' };
  return [shown, () => setAttempt((count) => count + 1), store, () => setAgain((count) => count + 1)];
}

// Beside the day panel the tiles are as tall as they are wide, to 78 px.
const TIGHT_TILE = 'clamp(60px, 7vw, 78px)';

function Tile({ entry, today, selected, compact, tight, mark, line, span, onPick }) {
  const { date, summary } = entry;
  const full = summary.state === 'full';
  const open = summary.state !== 'past' && summary.state !== 'closed';
  const reach = date >= today;
  const shut = closedAhead(summary);
  const night = line?.night ?? null;
  return <ButtonBase component="button" disabled={!reach} aria-pressed={selected}
    aria-label={tileName(date, summary, today, { inCart: mark === 'cart', nightBooked: night === 'other' || night === 'event', runs: line?.runs })}
    onClick={() => onPick(date)} sx={{
      position: 'relative', minHeight: compact ? 54 : tight ? TIGHT_TILE : 78, borderRadius: 2, p: compact ? 0.5 : 0.75,
      display: 'flex', flexDirection: 'column', alignItems: 'flex-start', justifyContent: 'flex-start', gap: '3px',
      bgcolor: open && !full ? 'background.paper' : 'transparent',
      border: `1px solid ${open ? gray[600] : 'transparent'}`,
      ...(shut && CLOSED_LOOK),
      outline: selected ? '2px solid' : 'none', outlineColor: '#eeeeec', outlineOffset: 1,
      opacity: open || shut || summary.mine || date === today ? 1 : reach ? 0.7 : 0.45,
      '&.Mui-disabled': { pointerEvents: 'none' },
    }}>
    <Typography component="span" variant="caption" sx={{ fontWeight: date === today ? 700 : 600, lineHeight: 1.4,
      ...dateSx({ full, mine: mark === 'mine', cart: mark === 'cart' }) }}>{Number(date.slice(8))}</Typography>
    {date === today && <Typography component="span" sx={{ fontSize: 10, lineHeight: 1, color: '#ff9592', fontWeight: 600 }}>{label('day.today')}</Typography>}
    <NightMoon mark={night} sx={{ position: 'absolute', top: 5, right: 4, fontSize: 15 }} />
    {line && <DayLine runs={line.runs} span={span} sx={{ position: 'absolute', left: 4, right: 4, bottom: 6 }} />}
  </ButtonBase>;
}

// The month's tiles, a week to a row with its number to the left (the demo's
// `MonthCalendar`); `built` is the month's calendar. Shared by the Month and the
// Week's jump to a date. `lines`: the days' lines when the caller has them.
export function MonthGrid({ built, month, selected, compact, tight = false, cart, lines: given = null, onPick }) {
  const days = cartDays(cart);
  const own = React.useMemo(() => (given ? null : dayLines(built.dates, cart)), [given, built, cart]);
  const lines = given ?? own;
  const span = React.useMemo(() => lineSpan(lines), [lines]);
  return <Box sx={{ display: 'grid', gridTemplateColumns: '22px repeat(7, minmax(0, 1fr))', gap: compact ? '3px' : '6px' }}>
    <span />
    {Array.from({ length: 7 }, (_, index) => <Typography key={index} variant="caption"
      sx={{ textAlign: 'center', color: 'text.secondary' }}>{compact ? weekdayInitial(index) : weekdayShort(index)}</Typography>)}
    {numberedWeeks(month).map(({ week, dates }) => <React.Fragment key={dates.find(Boolean)}>
      <Typography component="span" role="img" aria-label={label('week.number', { week })} variant="caption"
        sx={{ color: 'text.disabled', fontSize: 10, pt: 0.75, textAlign: 'right' }}>{week}</Typography>
      {dates.map((date, index) => {
        if (!date) return <span key={`blank-${index}`} />;
        const day = dayOn(built, date);
        if (!day) return <span key={date} />;
        return <Tile key={date} entry={day} today={built.today} selected={date === selected} compact={compact} tight={tight}
          mark={dateMark(day.summary, date, days)} line={lines.get(date)} span={span} onPick={onPick} />;
      })}
    </React.Fragment>)}
  </Box>;
}

export default function BookMonth({
  filter, wide, side = false, order = [], onChange, csrf, cart, onCart, refresh = 0, chosen = null, onChoose = () => {}, cartLink, pricesLink, initialMonth, onWindow,
}) {
  const query = calendarQuery(filter);
  const [today, setToday] = React.useState(browserToday);
  const [month, setMonth] = React.useState(() => initialMonth || monthOf(chosen || browserToday()));
  const shownMonth = clampMonth(month, today);
  React.useEffect(() => { onWindow?.(shownMonth); }, [shownMonth, onWindow]);
  const [state, retry, store, reload] = useMonthCalendar(query, shownMonth, filter.category, refresh);
  const [adding, setAdding] = React.useState(null);
  React.useEffect(() => {
    if (state.kind === 'ready') setToday(state.built.today);
  }, [state]);
  const move = (count) => { setMonth(shiftMonth(shownMonth, count)); onChoose(null); };
  const compact = !wide;
  const picked = usableDay(chosen, today);
  const selected = picked && monthOf(picked) === shownMonth ? picked : (monthOf(today) === shownMonth ? today : monthStart(shownMonth));
  const entry = state.kind === 'ready' ? dayOn(state.built, selected) : null;
  const built = state.kind === 'ready' ? state.built : null;
  const lines = React.useMemo(() => dayLines(built ? built.dates : [], cart), [built, cart]);

  const grid = <Box>
    <Stack direction="row" sx={{ alignItems: 'center', mb: 1 }}>
      <Button size="small" variant="outlined" sx={{ mr: 1, minWidth: 0, px: 1.5 }} onClick={() => { const target = todayTarget(today); setMonth(target.month); onChoose(target.chosen); }}>{label('day.today')}</Button>
      <IconButton size="small" aria-label={label('month.previous')} onClick={() => move(-1)}
        disabled={!canGoBack(shownMonth, today)}><ChevronLeftRounded /></IconButton>
      <Typography component="h2" sx={{ fontWeight: 600, flex: 1, textAlign: 'center' }}>{monthTitle(shownMonth)}</Typography>
      <IconButton size="small" aria-label={label('month.next')} onClick={() => move(1)}><ChevronRightRounded /></IconButton>
    </Stack>
    {state.kind === 'ready' && <MonthGrid built={state.built} month={shownMonth} selected={selected} compact={compact}
      cart={cart} lines={lines} onPick={onChoose} tight={side} />}
  </Box>;

  // The open dialog's container in the answer for the category now chosen (
  // the category changes in the dialog); the one it opened with until that is in.
  const fresh = adding ? (state.kind === 'ready' ? dayOn(state.built, adding.date)?.containers.find((row) => row.id === adding.container.id) : null) : null;
  const sheetContainer = fresh || adding?.container;
  const sheetKey = `${filter.category}${fresh ? '' : '-asked'}`;
  const recategorize = (category, choice) => {
    if (choice) setAdding((was) => ({ ...was, from: choice.start.at, length: (Date.parse(choice.end) - Date.parse(choice.start.at)) / 60000 }));
    onChange({ category }, true);
  };
  let panel = null;
  if (state.kind === 'loading') {
    panel = <Box sx={{ display: 'flex', justifyContent: 'center', py: 4 }}><CircularProgress aria-label={label('common.loading')} /></Box>;
  } else if (state.kind === 'error') {
    panel = <Alert severity="error" action={<Button color="inherit" size="small" onClick={retry}>{label('common.try-again')}</Button>}>
      {label('common.could-not-load')}
    </Alert>;
  } else if (entry) {
    panel = <Containers entry={entry} cart={cart} filter={filter} dense={compact} wide={wide} order={order} onChange={onChange}
      onAdd={(container, from) => setAdding({ container, date: entry.date, from })} />;
  }
  return <Stack spacing={2}>
    <Legend seen={legendSeen(built ? built.dates : [], lines)} />
    <Box sx={side ? { display: 'grid', gridTemplateColumns: 'minmax(0, 1.5fr) minmax(300px, 1fr)', gap: 3, alignItems: 'start' }
      : { display: 'flex', flexDirection: 'column', gap: 2 }}>
      {grid}
      {panel}
    </Box>
    {adding && <AddSheet key={sheetKey} container={sheetContainer} date={adding.date} from={adding.from} length={adding.length} filter={filter} csrf={csrf} wide={wide}
      store={store} onClose={() => setAdding(null)} toCart={cartLink} cart={cart} toPrices={pricesLink} onCategory={recategorize}
      onAdded={(next) => { setAdding(null); onCart(next); reload(); }} />}
  </Stack>;
}
