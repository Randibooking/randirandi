// SPDX-License-Identifier: AGPL-3.0-only
import React from 'react';
import {
  Box, Button, ButtonBase, CircularProgress, Dialog, IconButton, Stack, Typography,
} from '@mui/material';
import { alpha } from '@mui/material/styles';
import ChevronLeftRounded from '@mui/icons-material/ChevronLeftRounded';
import ChevronRightRounded from '@mui/icons-material/ChevronRightRounded';
import CloseRounded from '@mui/icons-material/CloseRounded';
import ExpandMoreRounded from '@mui/icons-material/ExpandMoreRounded';
import BedtimeOutlined from '@mui/icons-material/BedtimeOutlined';
import {
  brand, closed, gray, red,
} from '../theme/themePrimitives';
import { BookShown } from './AddSheet';
import { label } from './labels.js';
import { quantityOf } from './add.js';
import { bookingPriceWords, cartDays, dateMark } from './day.js';
import {
  BLOCKS, CLOSED_LOOK, EVENT_LINK, MonthGrid, closedAhead, dateSx, useMonthCalendar,
} from './BookMonth';
import { eventHref, inPage } from './places.js';
import { showBooked } from './BookedSheet';
import { calendarQuery } from './filter.js';
import { ROW_LEAST, rowHeight, scrollStart, weekRoom } from './fit.js';
import StackSheet from './StackSheet';
import { shiftMonth, monthOf, numberedWeeks } from './month.js';
import {
  closedReason, closedWords, dateWords, dayName, monthTitle, weekdayInitial,
} from './words.js';
import {
  dayLayout, freeRuns, hourInRun, hourMarks, laneBox, laneNames, nowLine, slotAction, slotProposal, slotWords, weekHours, weekLanes, wordLines,
} from './wide.js';

// The wide Week: seven columns, free time an hour at a time, the booking a click makes
// shown under the pointer in the main button's pale; the data is the server's.
const FREE = gray[50];
const LINE = alpha(gray[600], 0.5);
const SHADOW = '0 4px 12px rgba(0, 0, 0, .45)';
const GRID = '48px repeat(7, minmax(0, 1fr))';

const FREE_SLOT = {
  background: '#2a2a28', border: `1px solid #494844`, borderLeft: `3px solid #eeeeec`, color: '#eeeeec',
};
const dim = (date, today) => ({ opacity: date < today ? 0.45 : 1 });

// A blocked time is a quiet block with its word only (the demo's `BookedLabel`).
// Words wrap inside the slot's own height: an hour one line with an ellipsis,
// two hours two lines.
const clamp = (lines) => ({
  display: '-webkit-box', WebkitBoxOrient: 'vertical', WebkitLineClamp: lines, overflow: 'hidden', overflowWrap: 'anywhere',
});
function BlockWord({ children, lines = 1 }) {
  return <Typography sx={{ fontSize: 11, fontWeight: 500, lineHeight: '16px', pt: '3px', opacity: 0.8, ...clamp(lines) }}>{children}</Typography>;
}

// The booking a click makes, in the pale of the main button: the one bright thing.
function Ghost({ top, height, box, lines, children }) {
  return <Box aria-hidden sx={{
    position: 'absolute', ...box, top, height, zIndex: 1, pointerEvents: 'none', borderRadius: 1,
    bgcolor: FREE, color: gray[900], px: 0.5, overflow: 'hidden', boxShadow: SHADOW,
  }}>
    <Typography sx={{ fontSize: 11.5, fontWeight: 700, lineHeight: '16px', pt: '3px', ...clamp(lines) }}>
      {String(children).split('–').map((part, index) => <React.Fragment key={index}>{index > 0 && <>–<wbr /></>}{part}</React.Fragment>)}
    </Typography>
  </Box>;
}

function Column({ entry, layout, hours, row, today, at, elapsed, onSlot }) {
  const [hover, setHover] = React.useState(null);
  const y = (minute) => ((minute - hours.from) / 60) * row;
  const inside = (item) => item.stop > hours.from && item.start < hours.to;
  const lit = hover && layout.slots.find((slot) => slot.key === hover);
  const proposal = lit && slotProposal(entry.date, lit);
  const now = nowLine(entry.date, today, at, hours, elapsed);
  const box = (lane) => {
    const { left, width } = laneBox(lane, layout.lanes);
    return { left: `calc(${left} + 1px)`, width: `calc(${width} - 2px)` };
  };
  return <Box onMouseLeave={() => setHover(null)} sx={{
    position: 'relative', height: y(hours.to), borderLeft: 1, borderColor: 'divider',
    backgroundImage: `repeating-linear-gradient(to bottom, ${LINE} 0 1px, transparent 1px ${row}px)`,
  }}>
    {closedAhead(entry.summary) && <Box role="img" aria-label={`${dateWords(entry.date)}, ${closedWords(entry.summary.closed)}`}
      sx={{ position: 'absolute', inset: '1px', borderRadius: 1, px: 0.75, overflow: 'hidden', ...CLOSED_LOOK }}>
      {closedReason(entry.summary.closed) && <Typography sx={{ fontSize: 11, fontWeight: 500, lineHeight: '16px', pt: '3px', color: closed.text }}>
        {closedReason(entry.summary.closed)}</Typography>}
    </Box>}
    {freeRuns(layout.slots.filter(inside)).map((run) => {
      // The hour under the pointer; from the keyboard, the stretch's first.
      const under = (event) => {
        if (!event.clientY) return hourInRun(run, null);
        const rect = event.currentTarget.getBoundingClientRect();
        return hourInRun(run, run.start + ((event.clientY - rect.top + 2) / row) * 60);
      };
      return <ButtonBase key={run.key} component="button"
        aria-label={slotWords(entry.date, run.slots[0], layout.lanes > 1)}
        onMouseMove={(event) => setHover(under(event).key)} onFocus={() => setHover(run.slots[0].key)} onBlur={() => setHover(null)}
        onClick={(event) => onSlot(entry.date, under(event))}
        sx={{ position: 'absolute', ...box(run.lane), top: y(run.start) + 2, height: y(run.stop) - y(run.start) - 4,
          borderRadius: '6px', ...FREE_SLOT, display: 'block' }} />;
    })}
    {layout.blocks.filter(inside).map((block) => {
      const { sx } = BLOCKS[block.kind];
      const open = block.event
        ? { component: 'a', ...inPage(eventHref(block.event.id)), 'aria-label': [block.word, block.hours].join(', ') }
        : block.booked ? {
          role: 'button', tabIndex: 0, 'aria-haspopup': 'dialog', 'aria-label': [block.word, block.hours].join(', '),
          onClick: () => showBooked(block.booked),
          onKeyDown: (key) => { if (key.key === 'Enter' || key.key === ' ') { key.preventDefault(); showBooked(block.booked); } },
        } : { role: 'img', 'aria-label': [block.word, block.title].filter(Boolean).join(', ') };
      return <Box key={block.key} title={block.title} {...open}
        onMouseEnter={() => setHover(null)}
        sx={{ position: 'absolute', ...box(block.lane), top: y(Math.max(block.start, hours.from)) + 1,
          height: y(Math.min(block.stop, hours.to)) - y(Math.max(block.start, hours.from)) - 2,
          borderRadius: 1, px: 0.75, overflow: 'hidden', pointerEvents: block.over ? 'none' : 'auto', ...sx,
          ...(block.event && { display: 'block', ...EVENT_LINK }),
          ...(block.booked && { cursor: 'pointer', '&:hover': { boxShadow: 'inset 0 0 0 1px #eeeeec' }, '&:focus-visible': { outline: '2px solid #eeeeec', outlineOffset: '2px' } }) }}>
        <BlockWord lines={wordLines(block.stop - block.start)}>{block.word}</BlockWord>
      </Box>;
    })}
    {proposal && <Ghost box={box(lit.lane)} top={y(lit.start) + 1} lines={wordLines(proposal.minutes)}
      height={y(lit.start + proposal.minutes) - y(lit.start) - 2}>
      {proposal.words}</Ghost>}
    {now !== null && <Box sx={{ position: 'absolute', left: 0, right: 0, top: y(now), height: 2, bgcolor: red[400], zIndex: 2 }} />}
  </Box>;
}

function Night({ night, price, date, onAdd }) {
  const [lit, setLit] = React.useState(false);
  const free = night.state === 'free';
  const { sx } = free ? { sx: FREE_SLOT } : BLOCKS[night.state];
  const base = {
    mt: '3px', height: 30, borderRadius: 1, display: 'flex', alignItems: 'center', gap: 0.5, px: 0.75, overflow: 'hidden',
    whiteSpace: 'nowrap', width: '100%', justifyContent: 'flex-start', ...sx,
    ...(lit && { background: FREE, border: '1px solid transparent', color: gray[900], boxShadow: SHADOW }),
  };
  if (!free && night.booked) {
    return <ButtonBase component="button" aria-haspopup="dialog" aria-label={[night.word, night.hours].join(', ')}
      onClick={() => showBooked(night.booked)} sx={base}><BlockWord>{night.word}</BlockWord></ButtonBase>;
  }
  if (!free) {
    return <Box title={`${night.container.type.name} ${night.hours}`} sx={base}><BlockWord>{night.word}</BlockWord></Box>;
  }
  const words = price ? label('wide.book-night-price', { price }) : label('wide.book-night');
  return <ButtonBase component="button" aria-label={`${words}, ${dateWords(date)}`}
    onMouseEnter={() => setLit(true)} onMouseLeave={() => setLit(false)} onFocus={() => setLit(true)} onBlur={() => setLit(false)}
    onClick={() => onAdd({ container: night.container, date, from: night.at })} sx={base}>
    {!lit && <BedtimeOutlined sx={{ fontSize: 13, color: brand[200] }} />}
    <Typography sx={{ fontSize: 11.5, fontWeight: lit ? 700 : 600, flex: 1, textAlign: 'left' }}>{lit ? words : (price || '')}</Typography>
  </ButtonBase>;
}

// The month shown in the jump: the Month's own tiles, with the bar for what is
// left and the week numbers, from the month's calendar. Mounted only while the
// dialog is open, so a closed jump asks for nothing.
// A month's dates with no answer behind them: the date alone, not before today.
function PlainMonth({ month, today, selected, onDate }) {
  return <Box sx={{ display: 'grid', gridTemplateColumns: '22px repeat(7, minmax(0, 1fr))', gap: '3px' }}>
    <span />
    {Array.from({ length: 7 }, (_, index) => <Typography key={index} variant="caption"
      sx={{ textAlign: 'center', color: 'text.secondary' }}>{weekdayInitial(index)}</Typography>)}
    {numberedWeeks(month).map(({ week, dates }) => <React.Fragment key={dates.find(Boolean)}>
      <Typography component="span" role="img" aria-label={label('week.number', { week })} variant="caption"
        sx={{ color: 'text.disabled', fontSize: 10, pt: 0.75, textAlign: 'right' }}>{week}</Typography>
      {dates.map((date, index) => (date
        ? <ButtonBase key={date} component="button" disabled={date < today} aria-label={dateWords(date)}
          aria-pressed={date === selected} onClick={() => onDate(date)} sx={{
            minHeight: 54, borderRadius: 2, border: 1, borderColor: 'divider', opacity: date < today ? 0.4 : 1,
            outline: date === selected ? '2px solid' : 'none', outlineColor: '#eeeeec', outlineOffset: 1,
          }}>
          <Typography variant="caption" sx={{ fontWeight: date === today ? 700 : 600 }}>{Number(date.slice(8))}</Typography>
        </ButtonBase>
        : <span key={`blank-${index}`} />))}
    </React.Fragment>)}
  </Box>;
}

function JumpMonth({ month, today, selected, filter, cart, refresh, onDate }) {
  const [state] = useMonthCalendar(calendarQuery(filter), month, filter.category, refresh);
  if (state.kind === 'ready') {
    return <MonthGrid built={state.built} month={month} selected={selected} compact cart={cart}
      onPick={(date) => { if (date >= today) onDate(date); }} />;
  }
  // Without the month's calendar the dates are still a way to jump: plain tiles.
  if (state.kind === 'error') return <PlainMonth month={month} today={today} selected={selected} onDate={onDate} />;
  return <Box sx={{ display: 'flex', justifyContent: 'center', py: 4 }}><CircularProgress aria-label={label('common.loading')} /></Box>;
}

function Jump({ open: asked, selected, today, filter, cart, refresh = 0, onClose, onDate }) {
  const shown = React.useContext(BookShown);
  const open = asked && shown;
  const [month, setMonth] = React.useState(monthOf(selected));
  React.useEffect(() => { if (asked) setMonth(monthOf(selected)); }, [asked, selected]);
  return <Dialog open={open} onClose={onClose} fullWidth maxWidth="xs" aria-labelledby="jump-title"
    slotProps={{ paper: { sx: { bgcolor: 'background.paper', backgroundImage: 'none' } } }}>
    <Box sx={{ p: 3 }}>
      <Stack direction="row" sx={{ alignItems: 'center', mb: 1.5 }}>
        <Typography id="jump-title" variant="h6" sx={{ flex: 1, fontWeight: 600 }}>{label('jump.title')}</Typography>
        <IconButton size="small" onClick={onClose} aria-label={label('common.close')}><CloseRounded fontSize="small" /></IconButton>
      </Stack>
      <Stack direction="row" sx={{ alignItems: 'center', mb: 1 }}>
        <IconButton size="small" aria-label={label('month.previous')} disabled={month <= monthOf(today)}
          onClick={() => setMonth(shiftMonth(month, -1))}><ChevronLeftRounded /></IconButton>
        <Typography sx={{ flex: 1, textAlign: 'center', fontWeight: 600 }}>{monthTitle(month)}</Typography>
        <IconButton size="small" aria-label={label('month.next')} onClick={() => setMonth(shiftMonth(month, 1))}><ChevronRightRounded /></IconButton>
      </Stack>
      <JumpMonth month={month} today={today} selected={selected} filter={filter} cart={cart} refresh={refresh} onDate={onDate} />
    </Box>
  </Dialog>;
}

// The week's title, with the weeks before and after; it opens the date jump.
export function WideTitle({ title, canBack, onMove, onOpen, onToday }) {
  return <Stack direction="row" sx={{ alignItems: 'center', mb: 1 }}>
    <Button size="small" variant="outlined" sx={{ mr: 1, minWidth: 0, px: 1.5 }} onClick={onToday}>{label('day.today')}</Button>
    <IconButton size="small" aria-label={label('week.previous')} onClick={() => onMove(-1)} disabled={!canBack}><ChevronLeftRounded /></IconButton>
    <ButtonBase onClick={onOpen} aria-label={label('week.go-to-date', { title })} sx={{ flex: 1, gap: 0.25, borderRadius: 1, py: 0.5 }}>
      <Typography component="h2" sx={{ fontWeight: 600 }}>{title}</Typography>
      <ExpandMoreRounded fontSize="small" sx={{ color: 'text.secondary' }} />
    </ButtonBase>
    <IconButton size="small" aria-label={label('week.next')} onClick={() => onMove(1)}><ChevronRightRounded /></IconButton>
  </Stack>;
}

// The grid: the hour scale, seven columns, the Night row under the hours.
// A date's mark as `dateSx` takes it: the blue pill, the dashed ring, or none.
const markOf = (mark) => ({ mine: mark === 'mine', cart: mark === 'cart' });

export function WideGrid({ entries, today, at, elapsed = 0, selected, cart, filter, onAdd, below = 0 }) {
  const inCart = cartDays(cart);
  const lanes = weekLanes(entries);
  const laneIds = lanes.map((lane) => lane.id);
  const names = laneNames(lanes);
  const layouts = entries.map((entry) => dayLayout(entry, cart, laneIds));
  const hours = weekHours(entries);
  // A free night's price from the calendar's own prices: no question to the
  // server per night (part 3.98).
  const priceOf = (night) => bookingPriceWords(night.container, filter.category, quantityOf(night.container, filter),
    night.at, night.end, filter.group);
  const [stack, setStack] = React.useState(null);
  const onSlot = (date, slot) => {
    const action = slotAction(date, slot);
    if (action.add) onAdd(action.add);
    else setStack(action.stack);
  };
  const nights = layouts.some((layout) => layout.nights.length > 0);

  // The Week fills the window below what is above it and scrolls inside (part 3.9984):
  // the day headings above the hours and the Night row under them stay in view.
  const outer = React.useRef(null);
  const scroller = React.useRef(null);
  const head = React.useRef(null);
  const foot = React.useRef(null);
  const [fit, setFit] = React.useState(null);
  const measure = () => {
    if (!outer.current) return;
    const top = outer.current.getBoundingClientRect().top + window.scrollY;
    const next = { room: weekRoom(window.innerHeight, top, below), head: head.current?.offsetHeight || 0, foot: foot.current?.offsetHeight || 0 };
    setFit((was) => (was && was.room === next.room && was.head === next.head && was.foot === next.foot ? was : next));
  };
  React.useLayoutEffect(measure);
  React.useEffect(() => {
    window.addEventListener('resize', measure);
    return () => window.removeEventListener('resize', measure);
  }, []);
  const view = fit ? fit.room - fit.head - fit.foot - 16 : null;
  const row = hours && view !== null ? rowHeight(view, hours) : ROW_LEAST;
  const hoursHeight = hours ? ((hours.to - hours.from) / 60) * row : 0;
  const height = fit ? Math.min(fit.room, fit.head + fit.foot + hoursHeight + 16) : undefined;
  // On opening a week, the hours show from the first open hour, or from an hour before now.
  const placed = React.useRef(null);
  const week = `${entries[0].date}|${hours ? hours.from : ''}`;
  React.useLayoutEffect(() => {
    if (!hours || !fit || placed.current === week) return;
    placed.current = week;
    const now = entries.some((entry) => entry.date === today) ? nowLine(today, today, at, hours, elapsed) : null;
    scroller.current.scrollTop = scrollStart({ hours, row, now, view });
  }, [week, fit, row]);

  return <Box ref={outer} sx={{ position: 'relative' }}>
    <Box ref={scroller} sx={{ height, overflowY: 'auto', overflowX: 'hidden', overscrollBehavior: 'contain' }}>
      <Box ref={head} sx={{ position: 'sticky', top: 0, zIndex: 3, bgcolor: 'background.default', display: 'grid', gridTemplateColumns: GRID, columnGap: '6px' }}>
        <span />
        {entries.map((entry) => <Box key={entry.date} sx={{ textAlign: 'center', pb: 0.75, ...dim(entry.date, today) }}>
          <Typography variant="caption" sx={{ display: 'block', fontSize: 10.5, color: 'text.secondary', ...(entry.date === today && { color: '#ff9592', fontWeight: 600 }) }}>
            {dayName(entry.date, today)}</Typography>
          <Typography component="span" sx={{ display: 'inline-block', fontWeight: 600, fontSize: 17, lineHeight: 1.4,
            ...dateSx({ full: entry.summary.state === 'full', ...markOf(dateMark(entry.summary, entry.date, inCart)) }), }}>
            {Number(entry.date.slice(8))}</Typography>
        </Box>)}
        {names.length > 0 && <>
          <span />
          {entries.map((entry) => <Box key={entry.date} sx={{ display: 'flex', pb: 0.5, ...dim(entry.date, today) }}>
            {names.map((name, lane) => <Typography key={lane} title={name} noWrap variant="caption" sx={{
              flex: 1, minWidth: 0, textAlign: 'center', fontSize: 10, color: 'text.secondary' }}>{name}</Typography>)}
          </Box>)}
        </>}
      </Box>
      {hours && <Box sx={{ display: 'grid', gridTemplateColumns: GRID, columnGap: '6px', py: '8px' }}>
        <Box sx={{ position: 'relative', height: hoursHeight }}>
          {hourMarks(hours).map((mark) => <Typography key={mark.at} variant="caption" sx={{
            position: 'absolute', top: ((mark.at - hours.from) / 60) * row - 7, right: 3, fontSize: 10, color: 'text.secondary',
          }}>{mark.label}</Typography>)}
        </Box>
        {entries.map((entry, index) => <Box key={entry.date} sx={dim(entry.date, today)}>
          <Column entry={entry} layout={layouts[index]} hours={hours} row={row} today={today} at={at} elapsed={elapsed} onSlot={onSlot} />
        </Box>)}
      </Box>}
      {nights && <Box ref={foot} sx={{ position: 'sticky', bottom: 0, zIndex: 3, bgcolor: 'background.default', display: 'grid',
        gridTemplateColumns: GRID, columnGap: '6px', borderTop: 1, borderColor: 'divider', mt: '3px', pb: '2px' }}>
        <Stack sx={{ mt: '3px', alignItems: 'flex-end', justifyContent: 'center', pr: 0.375 }}>
          <Typography variant="caption" sx={{ fontSize: 10, color: 'text.secondary' }}>{label('wide.night')}</Typography>
        </Stack>
        {entries.map((entry, index) => <Box key={entry.date} sx={dim(entry.date, today)}>
          {layouts[index].nights.map((night) => <Night key={night.key} night={night} price={priceOf(night)} date={entry.date} onAdd={onAdd} />)}
        </Box>)}
      </Box>}
    </Box>
    {stack && <StackSheet wide {...stack} filter={filter} onClose={() => setStack(null)}
      onPick={(container, from) => { setStack(null); onAdd({ container, date: stack.date, from }); }} />}
  </Box>;
}

export { Jump };
