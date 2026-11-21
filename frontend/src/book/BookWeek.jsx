// SPDX-License-Identifier: AGPL-3.0-only
import React from 'react';
import {
  Alert, Box, Button, ButtonBase, CircularProgress, IconButton, Stack, Typography,
} from '@mui/material';
import ChevronLeftRounded from '@mui/icons-material/ChevronLeftRounded';
import ChevronRightRounded from '@mui/icons-material/ChevronRightRounded';
import { gray } from '../theme/themePrimitives';
import { readCalendar } from './api';
import AddSheet from './AddSheet';
import { calendarQuery } from './filter.js';
import { label } from './labels.js';
import {
  inTurn, keyOf, settle,
} from './store.js';
import { cartDays, dateMark } from './day.js';
import { calendars } from './kept.js';
import { addDays, buildCalendar } from './model.js';
import { dayOn, readDays, todayTarget } from './month.js';
import {
  canGoBackWeek, chipState, chosenDay, windowStart, usableDay, weekAsks, weekDays, weekTitle, weekToAsk,
} from './week.js';
import { dateWords, dayName, isEmptyDay, tileName } from './words.js';
import { Jump, WideGrid, WideTitle } from './WideWeek';
import { jumpTo } from './wide.js';
import {
  CLOSED_LOOK, Containers, DayLine, Legend, NightMoon, browserToday, closedAhead, dateSx,
} from './BookMonth';
import { dayLines, legendSeen, lineSpan } from './dayline.js';

// The strip of the week's days above the chosen day is the phone's Week; the
// chosen day below it is the Month's own day panel (`Containers`), and the days are the
// server's: one whole-month answer per month the week touches, kept under the
// same question as the Month's (store.js), so months the Month has seen show at once.
const FREE = gray[50];

// The week's month answers: those kept at once, the rest asked; a failed read
// keeps what is kept. `refresh` and `again` ask again quietly.
function useWeekCalendars(query, start, category, refresh) {
  const [attempt, setAttempt] = React.useState(0);
  const [again, setAgain] = React.useState(0);
  const [store] = React.useState(() => calendars);
  const [failed, setFailed] = React.useState(null);
  const [, setTick] = React.useState(0);
  const asks = weekAsks(start);
  const asked = JSON.stringify([query, start, category, attempt]);
  const forced = React.useRef(JSON.stringify([refresh, again, attempt]));
  React.useEffect(() => {
    let live = true;
    setFailed(null);
    // A return to Book and Try again ask every month; a plain move only the unheld.
    const token = JSON.stringify([refresh, again, attempt]);
    const force = forced.current !== token;
    forced.current = token;
    const ask = weekToAsk(weekAsks(start), (row) => store.has(keyOf(query, row.start, row.days)), force);
    // One at a time, and each shared with an ask for the same month already on its
    // way (the Month's quiet fetch of its neighbour): a switch sends no month twice.
    inTurn(ask, async ({ start, days }) => {
      const result = await store.ask(keyOf(query, start, days), () => readDays(readCalendar, query, start, days));
      if (result.kind === 'signed-out') {
        if (live) window.location.assign('/login/');
        return true;
      }
      const key = keyOf(query, start, days);
      const built = settle(store, key, result, (data) => buildCalendar(data, { start, days, category }));
      return Boolean(built);
    }, () => live).then((oks) => {
      if (!live) return;
      setFailed(oks.length === ask.length && oks.every(Boolean) ? null : asked);
      setTick((count) => count + 1);
    });
    return () => { live = false; };
  }, [asked, refresh, again]);
  const builts = asks.map(({ start, days }) => store.get(keyOf(query, start, days)));
  return {
    builts, store, failed: failed === asked && builts.some((row) => !row),
    retry: () => setAttempt((count) => count + 1), reload: () => setAgain((count) => count + 1),
  };
}

// A day of the week: its name, date, and the Month's line and moon (T3,
// 2026-11-15) on the week's shared span.
function Chip({ date, entry, today, selected, mark, line, span, onPick }) {
  const state = chipState(entry, date, today);
  // A closed day that is not past wears the legend's Closed look, at full strength.
  const shut = Boolean(entry) && closedAhead(entry.summary);
  const night = line?.night ?? null;
  return <ButtonBase component="button" disabled={!state.reach} aria-pressed={selected}
    aria-label={entry ? tileName(date, entry.summary, today, { nightBooked: night === 'other' || night === 'event', runs: line?.runs }) : dateWords(date)} onClick={() => onPick(date)} sx={{
      display: 'block', textAlign: 'center', borderRadius: 2, py: 0.5, border: 1, borderColor: 'divider',
      opacity: state.dim && !shut ? 0.4 : 1,
      ...(shut && CLOSED_LOOK),
      // The chosen day is outlined, never filled: blue means yours.
      outline: selected ? '2px solid' : 'none', outlineColor: '#eeeeec', outlineOffset: 1,
      '&.Mui-disabled': { pointerEvents: 'none' },
    }}>
    <Typography variant="caption" sx={{ display: 'block', color: 'text.secondary', fontSize: 10, ...(date === today && { color: '#ff9592', fontWeight: 600 }) }}>
      {dayName(date, today)}<NightMoon mark={night} sx={{ fontSize: 12, ml: '2px', verticalAlign: '-2px' }} /></Typography>
    <Typography component="span" sx={{ display: 'inline-block', fontWeight: date === today ? 700 : 600, fontSize: 15, lineHeight: 1.3,
      ...dateSx({ full: state.full, mine: mark === 'mine', cart: mark === 'cart' }) }}>{Number(date.slice(8))}</Typography>
    {line && <DayLine runs={line.runs} span={span} sx={{ mx: 0.5, mt: 0.5, mb: 0.25 }} />}
  </ButtonBase>;
}

export default function BookWeek({ filter, wide, order = [], onChange, csrf, cart, onCart, refresh = 0, chosen = null, onChoose = () => {}, cartLink, pricesLink, below = 0, initialFrom, onWindow }) {
  const inCart = cartDays(cart);
  const query = calendarQuery(filter);
  const [today, setToday] = React.useState(browserToday);
  const [from, setFrom] = React.useState(() => initialFrom || chosen || browserToday());
  const start = windowStart(from, today);
  React.useEffect(() => { onWindow?.(start); }, [start, onWindow]);
  const [adding, setAdding] = React.useState(null);
  const [jumping, setJumping] = React.useState(false);
  const { builts, store, failed, retry, reload } = useWeekCalendars(query, start, filter.category, refresh);
  const known = builts.find(Boolean);
  React.useEffect(() => {
    if (known) setToday(known.today);
  }, [known]);
  const ready = builts.every(Boolean);
  const days = weekDays(start);
  const selected = chosenDay(start, usableDay(chosen, today), today);
  const entryOf = (date) => builts.reduce((found, built) => found || dayOn(built, date), null);
  const entry = entryOf(selected);
  // A move chooses the new week's first day that is not past (Monday, or today in
  // the current week), so Month then opens on the week's month.
  const move = (count) => {
    const next = addDays(start, 7 * count);
    setFrom(next);
    onChoose(next < today ? today : next);
  };

  const goToday = () => {
    const target = todayTarget(today);
    setFrom(target.from);
    onChoose(target.chosen);
  };
  const jump = (date) => {
    const target = jumpTo(date, today);
    setFrom(target.first);
    onChoose(target.chosen);
    setJumping(false);
  };
  const entries = days.map(entryOf);
  // Seven days: worked out once a render, for the chips, their span and the legend.
  const lines = dayLines(entries, cart);
  const span = lineSpan(lines);
  // The red line's clock: the answer that holds today, moved on by the time since it was read.
  const source = builts.find((built) => built && dayOn(built, today)) || known;
  const [, setMinute] = React.useState(0);
  React.useEffect(() => {
    if (!wide) return undefined;
    const timer = setInterval(() => setMinute((count) => count + 1), 60000);
    return () => clearInterval(timer);
  }, [wide]);
  const emptyWeek = ready && entries.every((one) => !one || isEmptyDay(one));

  // The open dialog's container in the answer for the category now chosen (
  // the category changes in the dialog); the one it opened with until that is in.
  const fresh = adding ? entryOf(adding.date)?.containers.find((row) => row.id === adding.container.id) : null;
  const sheetContainer = fresh || adding?.container;
  const sheetKey = `${filter.category}${fresh ? '' : '-asked'}`;
  const recategorize = (category, choice) => {
    if (choice) setAdding((was) => ({ ...was, from: choice.start.at, length: (Date.parse(choice.end) - Date.parse(choice.start.at)) / 60000 }));
    onChange({ category }, true);
  };
  let panel = null;
  if (failed) {
    panel = <Alert severity="error" action={<Button color="inherit" size="small" onClick={retry}>{label('common.try-again')}</Button>}>
      {label('common.could-not-load')}
    </Alert>;
  } else if (ready && entry && (!wide || emptyWeek)) {
    panel = <Containers entry={entry} cart={cart} filter={filter} dense={!wide} wide={wide} order={order} onChange={onChange}
      onAdd={(container, from) => setAdding({ container, date: entry.date, from })} />;
  } else if (ready && wide && entries.every(Boolean)) {
    panel = <WideGrid entries={entries} today={today} at={source.at} elapsed={Date.now() - source.readAt} selected={selected} cart={cart} filter={filter}
      onAdd={setAdding} below={below} />;
  } else {
    panel = <Box sx={{ display: 'flex', justifyContent: 'center', py: 4 }}><CircularProgress aria-label={label('common.loading')} /></Box>;
  }
  if (wide) {
    return <Stack spacing={2}>
      <Legend seen={legendSeen(entries, lines)} />
      <Box>
        <WideTitle title={weekTitle(start)} canBack={canGoBackWeek(start, today)} onMove={move} onOpen={() => setJumping(true)}
          onToday={goToday} />
        {panel}
      </Box>
      <Jump open={jumping} selected={selected} today={today} filter={filter} cart={cart} refresh={refresh} onClose={() => setJumping(false)} onDate={jump} />
      {adding && <AddSheet key={sheetKey} container={sheetContainer} date={adding.date} from={adding.from} length={adding.length} filter={filter} csrf={csrf} wide={wide}
        store={store} onClose={() => setAdding(null)} toCart={cartLink} cart={cart} toPrices={pricesLink} onCategory={recategorize}
        onAdded={(next) => { setAdding(null); onCart(next); reload(); }} />}
    </Stack>;
  }
  return <Stack spacing={2}>
    <Legend seen={legendSeen(entries, lines)} />
    <Box sx={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
      <Box>
        <Stack direction="row" sx={{ alignItems: 'center', mb: 1 }}>
          <Button size="small" variant="outlined" sx={{ mr: 1, minWidth: 0, px: 1.5 }} onClick={goToday}>{label('day.today')}</Button>
          <IconButton size="small" aria-label={label('week.previous')} onClick={() => move(-1)}
            disabled={!canGoBackWeek(start, today)}><ChevronLeftRounded /></IconButton>
          <Typography component="h2" sx={{ fontWeight: 600, flex: 1, textAlign: 'center' }}>{weekTitle(start)}</Typography>
          <IconButton size="small" aria-label={label('week.next')} onClick={() => move(1)}><ChevronRightRounded /></IconButton>
        </Stack>
        {ready && <Box sx={{ display: 'grid', gridTemplateColumns: 'repeat(7, 1fr)', gap: '4px' }}>
          {days.map((date) => <Chip key={date} date={date} entry={entryOf(date)} today={today}
            mark={dateMark(entryOf(date)?.summary, date, inCart)} selected={date === selected} line={lines.get(date)} span={span} onPick={onChoose} />)}
        </Box>}
      </Box>
      {panel}
    </Box>
    {adding && <AddSheet key={sheetKey} container={sheetContainer} date={adding.date} from={adding.from} length={adding.length} filter={filter} csrf={csrf} wide={wide}
      store={store} onClose={() => setAdding(null)} toCart={cartLink} cart={cart} toPrices={pricesLink} onCategory={recategorize}
      onAdded={(next) => { setAdding(null); onCart(next); reload(); }} />}
  </Stack>;
}
