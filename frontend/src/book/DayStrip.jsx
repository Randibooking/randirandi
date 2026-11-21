// SPDX-License-Identifier: AGPL-3.0-only
import React from 'react';
import { Box, ButtonBase, Slider, Typography } from '@mui/material';
import { bookedStripes, closed, closedStripes } from '../theme/themePrimitives';
import { label } from './labels.js';
import { clockOf } from './stack.js';
import { dateWords, whoIn } from './words.js';

// The container's day inside the booking dialog ("I should let you book
// inside the container"): its own hours as one strip in the calendar's look (a
// free stretch with its white stripe, ; what takes the rest in its own
// colour and word) and the time being chosen on top, in the main button's
// pale (). Nothing around the container is drawn ("A - NONE").
// Each free hour is a button: it moves the start there and keeps the length
// where it fits; two handles on the same strip adjust the selected span.
const MINUTE = 60000;
const minuteOf = (date, moment) => (Date.parse(moment.slice(0, 10)) - Date.parse(date)) / MINUTE
  + Number(moment.slice(11, 13)) * 60 + Number(moment.slice(14, 16));
const FIRM = ['yours', 'cart', 'lost', 'held', 'other', 'event', 'block'];

const LOOK = {
  free: { background: '#2a2a28', boxShadow: 'inset 0 0 0 1px #494844, inset 0 -3px 0 #eeeeec' },
  other: { background: bookedStripes(5, 7), boxShadow: 'inset 0 0 0 1px #494844', color: '#eeeeec' },
  event: { background: '#302008', boxShadow: 'inset 0 0 0 1px #714f19', color: '#ffe7b3' },
  closed: { background: closedStripes(5, 7), backgroundColor: closed.hatch,
    outline: `1px dashed ${closed.edge}`, outlineOffset: '-1px', color: closed.text },
  yours: { background: '#084843', boxShadow: 'inset 0 0 0 1px #12a594', color: '#adf0dd' },
  cart: { background: 'transparent', outline: '1px dashed #12a594', outlineOffset: '-1px', color: '#eeeeec' },
  blank: { background: 'transparent', boxShadow: 'inset 0 0 0 1px #3b3a37' },
};
const wordOf = (state, event, name) => ({
  other: name || label('state.booked'), yours: label('state.yours'), cart: label('state.cart'), closed: label('state.closed'), event: event?.title ?? label('state.event'),
})[state] ?? '';

// Each row of the container's day: free where the server offers a start in it,
// else what takes it (the firmest), else blank (closed, passed, too short).
function cellsOf(container, date) {
  const cells = [];
  for (const period of container.periods) {
    const starts = new Set(period.starts.map((start) => start.row));
    period.rows.forEach((row, index) => {
      const kinds = period.columns.map((column) => column.kinds[index]).filter((kind) => kind !== 'blank');
      let state = 'blank';
      let event = null;
      let name = '';
      let booking = '';
      if (kinds.includes('free')) state = starts.has(index) ? 'free' : 'blank';
      else if (kinds.length) state = kinds.reduce((a, b) => (FIRM.indexOf(a) <= FIRM.indexOf(b) ? a : b));
      if (state === 'block') state = 'closed';
      else if (state === 'held' || state === 'lost') {
        ({ name, booking } = whoIn(period, index, state === 'held' ? 'held' : 'other'));
        state = 'other';
      } else if (state === 'other') ({ name, booking } = whoIn(period, index, 'other'));
      if (state === 'event') event = period.columns.map((column) => column.events?.[index]).find(Boolean) ?? null;
      cells.push({ from: minuteOf(date, row.at), to: minuteOf(date, row.end), state, at: row.at, event, name, booking });
    });
  }
  return cells.sort((a, b) => a.from - b.from);
}

export default function DayStrip({ container, date, choice, onStart, slider, onSlide }) {
  const cells = React.useMemo(() => cellsOf(container, date), [container, date]);
  if (!cells.length) return null;
  const first = cells[0].from;
  const last = cells[cells.length - 1].to;
  const at = (minute) => `${((minute - first) / (last - first)) * 100}%`;
  const across = (from, to) => `${((to - from) / (last - first)) * 100}%`;
  const segments = [];
  for (const cell of cells) {
    const previous = segments[segments.length - 1];
    if (previous && previous.state === cell.state && previous.to === cell.from && (previous.event?.id ?? null) === (cell.event?.id ?? null)
      && previous.booking === cell.booking) {
      previous.to = cell.to;
      previous.count += 1;
    } else segments.push({ state: cell.state, from: cell.from, to: cell.to, count: 1, event: cell.event, name: cell.name, booking: cell.booking });
  }
  const fixed = container.periodType === 'fixed';
  const pick = choice ? [minuteOf(date, choice.start.at), minuteOf(date, choice.end)] : null;
  const step = last - first > 6 * 60 ? 120 : 60;
  const ticks = [];
  if (fixed) ticks.push(first, last);
  else for (let minute = Math.ceil(first / step) * step; minute <= last; minute += step) ticks.push(minute);
  const shift = (minute) => {
    if (minute === first) return 'none';
    return minute === last ? 'translateX(-100%)' : 'translateX(-50%)';
  };
  return <Box role="group" aria-label={label('add.strip', { type: container.type.name, date: dateWords(date) })}>
    <Box sx={{ position: 'relative', height: 44 }}>
      {segments.map((segment) => <Box key={segment.from} aria-hidden sx={{
        position: 'absolute', top: 0, bottom: 0, left: `calc(${at(segment.from)} + 1px)`, width: `calc(${across(segment.from, segment.to)} - 2px)`,
        borderRadius: '6px', overflow: 'hidden', px: 0.75, display: 'flex', alignItems: 'center', ...LOOK[segment.state],
      }}>
        {segment.count >= 2 && <Typography noWrap sx={{ fontSize: 11, fontWeight: 500 }}>{wordOf(segment.state, segment.event, segment.name)}</Typography>}
      </Box>)}
      {pick && <Box aria-hidden sx={{
        position: 'absolute', top: -3, bottom: -3, left: `calc(${at(pick[0])} + 1px)`, width: `calc(${across(pick[0], pick[1])} - 2px)`, zIndex: 1,
        borderRadius: '7px', bgcolor: '#eeeeec', color: '#111110', boxShadow: '0 4px 12px rgba(0, 0, 0, .45)',
        display: 'flex', alignItems: 'center', justifyContent: 'center', overflow: 'hidden',
      }} />}
      {cells.filter((cell) => cell.state === 'free').map((cell) => <ButtonBase key={cell.from} component="button"
        aria-label={label('add.strip-start', { time: clockOf(cell.from) })} aria-pressed={Boolean(pick && pick[0] === cell.from)}
        onClick={() => onStart(cell.at)}
        sx={{ position: 'absolute', top: 0, bottom: 0, left: at(cell.from), width: across(cell.from, cell.to), zIndex: 2, borderRadius: '6px',
          '&:hover': { boxShadow: 'inset 0 0 0 2px #eeeeec' },
          '&.Mui-focusVisible': { outline: '2px solid #eeeeec', outlineOffset: '1px' } }} />)}
      {slider && <Slider value={slider.value} min={slider.from} max={slider.to} step={slider.step} disableSwap
        track={false} valueLabelDisplay="off" getAriaValueText={clockOf}
        getAriaLabel={(index) => label(index === 0 ? 'add.start' : 'add.end')}
        onChange={(_, value, thumb) => onSlide(value, thumb)}
        sx={{
          position: 'absolute', top: 0, left: at(slider.from), width: across(slider.from, slider.to),
          height: 44, p: 0, zIndex: 3, pointerEvents: 'none',
          '@media (pointer: coarse)': { padding: 0 },
          '& .MuiSlider-rail': { display: 'none' },
          '& .MuiSlider-thumb': {
            pointerEvents: 'auto', width: 12, height: 36, borderRadius: '4px',
            bgcolor: '#eeeeec', border: '2px solid #222221', boxShadow: '0 0 0 1px #eeeeec',
            '&::after': { width: 44, height: 48 },
            '&:hover, &.Mui-focusVisible, &.Mui-active': { boxShadow: '0 0 0 4px rgba(238,238,236,.3)' },
          },
        }} />}
    </Box>
    <Box aria-hidden sx={{ position: 'relative', height: 16, mt: 0.5 }}>
      {ticks.map((minute) => <Typography key={minute} sx={{ position: 'absolute', left: at(minute), transform: shift(minute),
        fontSize: 11, lineHeight: '16px', color: '#b5b3ad' }}>{fixed ? clockOf(minute) : clockOf(minute).slice(0, 2)}</Typography>)}
    </Box>
  </Box>;
}
