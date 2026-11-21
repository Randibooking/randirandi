// SPDX-License-Identifier: AGPL-3.0-only
import React from 'react';
import {
  Alert, Box, Button, Chip, CircularProgress, Link, List, ListItemButton, Stack, ToggleButton, ToggleButtonGroup, Typography,
} from '@mui/material';
import CelebrationOutlined from '@mui/icons-material/CelebrationOutlined';
import ChevronLeftRounded from '@mui/icons-material/ChevronLeftRounded';
import ChevronRightRounded from '@mui/icons-material/ChevronRightRounded';
import CloseRounded from '@mui/icons-material/CloseRounded';
import EventRepeatRounded from '@mui/icons-material/EventRepeatRounded';
import GroupsOutlined from '@mui/icons-material/GroupsOutlined';
import HandymanOutlined from '@mui/icons-material/HandymanOutlined';
import MailOutlineRounded from '@mui/icons-material/MailOutlineRounded';
import MeetingRoomOutlined from '@mui/icons-material/MeetingRoomOutlined';
import PhoneOutlined from '@mui/icons-material/PhoneOutlined';
import PlaceOutlined from '@mui/icons-material/PlaceOutlined';
import ScheduleRounded from '@mui/icons-material/ScheduleRounded';
import TuneRounded from '@mui/icons-material/TuneRounded';
import { readEvents } from './api';
import { browserToday } from './BookMonth';
import { Row, Sheet } from './FilterControls';
import {
  bookDay, CATEGORIES, categoryWord, dayWords, daysArePast, daysOf, eventList, eventsAddress, eventsQuery, placeChoices,
  placeName, readEventsAddress, whenWords,
} from './events.js';
import { label } from './labels.js';
import { eventBack, showDay } from './places.js';

// The members' Events page : every event of the
// organization, with and without an asset; a repeating event once, under Every
// week; the others by month; Coming and Past; the Filter by category and where;
// an event's page; on a wide screen the list beside the chosen event. An event
// is amber wherever it shows, as on Book (), and the category is in the
// organization's own words . What it shows is the server's events answer.
export const CATEGORY_ICONS = {
  social: CelebrationOutlined, volunteer_work: HandymanOutlined, organization: GroupsOutlined, open_house: MeetingRoomOutlined,
};
const CARD = { border: 1, borderColor: 'divider', borderRadius: 2, bgcolor: 'background.paper' };
const AMBER = { bgcolor: '#302008', color: '#ffe7b3', border: '1px solid #714f19' };
const chipX = { '& .MuiChip-deleteIcon': { fontSize: 16, color: 'text.secondary', '&:hover': { color: 'text.primary' } } };
// Book's Filter rows, flat (FilterControls.jsx `choices`).
const CHOICES = { mt: 0.5, boxShadow: 'none', '& .MuiToggleButton-root': { boxShadow: 'none', py: 0.75 } };

function DateBadge({ date, repeat }) {
  return <Box aria-hidden sx={{ width: 48, flexShrink: 0, borderRadius: 1.5, py: 0.5, textAlign: 'center', alignSelf: 'center', ...AMBER }}>
    {repeat ? <EventRepeatRounded sx={{ fontSize: 22, my: '7px', display: 'block', mx: 'auto' }} /> : <>
      <Typography sx={{ fontSize: 11, lineHeight: 1.4, fontWeight: 600 }}>{dayWords(date).split(' ')[0]}</Typography>
      <Typography sx={{ fontSize: 20, lineHeight: 1.15, fontWeight: 700 }}>{Number(date.slice(8))}</Typography>
    </>}
  </Box>;
}

function EventRow({ row, chosen, link }) {
  const { event } = row;
  const Icon = CATEGORY_ICONS[event.category];
  const to = link({ view: 'event', event: event.id });
  return <ListItemButton component="a" href={to.href} onClick={to.onClick} selected={chosen} aria-current={chosen ? 'true' : undefined}
    sx={{ gap: 1.5, py: 1.25, px: 1.5, '& + &': { borderTop: 1, borderColor: 'divider' } }}>
    <DateBadge date={row.date} repeat={Boolean(event.repeat)} />
    <Box sx={{ flex: 1, minWidth: 0 }}>
      <Typography noWrap sx={{ fontWeight: 600 }}>{event.title}</Typography>
      <Stack direction="row" spacing={0.5} sx={{ alignItems: 'center', color: 'text.secondary', minWidth: 0 }}>
        <Icon sx={{ fontSize: 16, flexShrink: 0 }} />
        <Typography variant="body2" noWrap>{[categoryWord(event.category), event.asset?.name].filter(Boolean).join(' · ')}</Typography>
      </Stack>
      <Typography variant="body2" sx={{ color: 'text.secondary' }}>{row.hours}</Typography>
    </Box>
    <ChevronRightRounded sx={{ color: 'text.secondary', flexShrink: 0 }} />
  </ListItemButton>;
}

function Section({ title, children }) {
  return <Box component="section">
    <Typography component="h2" variant="subtitle2" sx={{ color: 'text.secondary', fontWeight: 600, mb: 0.75 }}>{title}</Typography>
    <List disablePadding sx={{ ...CARD, overflow: 'hidden' }}>{children}</List>
  </Box>;
}

// Reuse Book's filter pattern: choices per part followed by Show.
// Categories allow multiple choices (All first); place allows one.
function EventsFilter({ open, phone, filter, places, onChange, onClose }) {
  const kinds = filter.categories.length ? filter.categories : ['all'];
  const pick = (next) => onChange({ categories: next.includes('all') && !kinds.includes('all') ? [] : CATEGORIES.filter((key) => next.includes(key)) });
  return <Sheet open={open} phone={phone} onClose={onClose}>
    <Stack spacing={2}>
      <Box>
        <Typography variant="caption" sx={{ color: 'text.secondary' }}>{label('events.category')}</Typography>
        <ToggleButtonGroup orientation="vertical" fullWidth size="small" value={kinds} aria-label={label('events.category')} sx={CHOICES}
          onChange={(_, next) => pick(next)}>
          <ToggleButton value="all">{label('events.all')}</ToggleButton>
          {CATEGORIES.map((key) => <ToggleButton key={key} value={key}>{categoryWord(key)}</ToggleButton>)}
        </ToggleButtonGroup>
      </Box>
      <Row caption={label('events.where')} value={filter.where} options={placeChoices(places)} onPick={(where) => onChange({ where })} />
      <Button variant="contained" onClick={onClose}>{label('filter.show')}</Button>
    </Stack>
  </Sheet>;
}

function Line({ Icon, caption, children }) {
  return <Stack direction="row" spacing={1.5} sx={{ py: 1.25, '& + &': { borderTop: 1, borderColor: 'divider' } }}>
    <Icon sx={{ fontSize: 20, color: 'text.secondary', mt: '2px' }} />
    <Box sx={{ minWidth: 0 }}>
      <Typography variant="caption" sx={{ color: 'text.secondary' }}>{caption}</Typography>
      {children}
    </Box>
  </Stack>;
}

function EventDetail({ event, today, admin, wide }) {
  const [more, setMore] = React.useState(false);
  React.useEffect(() => setMore(false), [event.id]);
  const Icon = CATEGORY_ICONS[event.category];
  const days = event.repeat ? daysOf(event, more ? 16 : 6) : null;
  const { contact } = event;
  const toBook = (click) => { click.preventDefault(); showDay(bookDay(event, today)); };
  return <Stack spacing={2}>
    <Box>
      <Stack direction="row" spacing={0.75} sx={{ alignItems: 'center', color: 'text.secondary', mb: 0.5 }}>
        <Icon sx={{ fontSize: 18 }} />
        <Typography variant="body2">{categoryWord(event.category)}</Typography>
      </Stack>
      <Typography component={wide ? 'h2' : 'h1'} variant="h4">{event.title}</Typography>
    </Box>
    <Box sx={{ ...CARD, px: 2, py: 0.5 }}>
      <Line Icon={ScheduleRounded} caption={label('events.page-when')}><Typography>{whenWords(event)}</Typography></Line>
      {event.asset && <Line Icon={PlaceOutlined} caption={label('events.page-where')}>
        <Typography>{event.asset.name} · <Link href="/book/" onClick={toBook} sx={{ color: 'text.primary' }}>{label('events.see-in-book')}</Link></Typography>
      </Line>}
      {event.description && <Typography sx={{ py: 1.25, borderTop: 1, borderColor: 'divider' }}>{event.description}</Typography>}
    </Box>
    {contact && <Box sx={{ ...CARD, px: 2, py: 1.5 }}>
      <Typography variant="caption" sx={{ color: 'text.secondary' }}>{label('events.contact')}</Typography>
      <Typography sx={{ fontWeight: 600 }}>{contact.name}</Typography>
      {contact.email && <Stack direction="row" spacing={1} sx={{ alignItems: 'center', mt: 0.5 }}>
        <MailOutlineRounded sx={{ fontSize: 18, color: 'text.secondary' }} />
        <Link href={`mailto:${contact.email}`} sx={{ color: 'text.primary' }}>{contact.email}</Link>
      </Stack>}
      {contact.phone && <Stack direction="row" spacing={1} sx={{ alignItems: 'center', mt: 0.5 }}>
        <PhoneOutlined sx={{ fontSize: 18, color: 'text.secondary' }} />
        <Link href={`tel:${contact.phone.replace(/ /g, '')}`} sx={{ color: 'text.primary' }}>{contact.phone}</Link>
      </Stack>}
    </Box>}
    {days && days.length > 0 && <Box sx={{ ...CARD, px: 2, py: 1.5 }}>
      <Typography component="h3" variant="caption" sx={{ color: 'text.secondary', display: 'block', mb: 0.5 }}>
        {label(daysArePast(event, today) ? 'events.past-days' : 'events.coming-days')}
      </Typography>
      {days.map((day) => <Stack key={day.date} direction="row" sx={{ alignItems: 'center', py: 0.75, '& + &': { borderTop: 1, borderColor: 'divider' } }}>
        <Typography sx={{ flex: 1, ...(day.cancelled && { color: 'text.secondary' }) }}>{dayWords(day.date)}</Typography>
        {day.cancelled ? <Chip size="small" label={label('events.cancelled')} variant="outlined" />
          : <Typography sx={{ fontVariantNumeric: 'tabular-nums' }}>{`${day.from}–${day.to}`}</Typography>}
      </Stack>)}
      {!more && event.days.length > 6 && <Button size="small" onClick={() => setMore(true)} sx={{ mt: 0.5, ml: -1 }}>{label('events.more-days')}</Button>}
    </Box>}
    {admin && <Box><Button variant="outlined" size="small" href={`/admin/events/${event.id}/`}>{label('events.change-in-admin')}</Button></Box>}
  </Stack>;
}

// What the server answers for a question, kept while the next is asked.
function useEvents(query) {
  const [state, setState] = React.useState({ kind: 'loading' });
  const [attempt, setAttempt] = React.useState(0);
  const key = JSON.stringify(query);
  React.useEffect(() => {
    if (!query) return undefined;
    let live = true;
    setState((was) => (was.kind === 'ready' ? was : { kind: 'loading' }));
    readEvents(query).then((result) => {
      if (!live) return;
      if (result.kind === 'signed-out') window.location.assign('/login/');
      else if (result.ok) setState({ kind: 'ready', data: result.data });
      else setState({ kind: 'error' });
    });
    return () => { live = false; };
  }, [key, attempt]);
  return [state, () => setAttempt((count) => count + 1)];
}

function Waiting() {
  return <Box sx={{ display: 'flex', justifyContent: 'center', py: 4 }}><CircularProgress aria-label={label('common.loading')} /></Box>;
}

function Failed({ onRetry }) {
  return <Alert severity="error" action={<Button color="inherit" size="small" onClick={onRetry}>{label('common.try-again')}</Button>}>
    {label('common.could-not-load')}
  </Alert>;
}

// One event by its id, from the list when it is there, else asked for alone
// (a link from Book to a day long past, or one the Filter leaves out).
function useOne(id, listed) {
  const inList = id ? listed.find((event) => event.id === id) : null;
  const [state, retry] = useEvents(id && !inList ? { event: id } : null);
  if (!id) return [{ kind: 'none' }, retry];
  if (inList) return [{ kind: 'ready', event: inList }, retry];
  if (state.kind !== 'ready') return [state, retry];
  return [{ kind: 'ready', event: state.data.events[0] ?? null }, retry];
}

export default function EventsPage({ config, route, link, wide }) {
  const today = browserToday();
  const [filter, setFilter] = React.useState(() => readEventsAddress(window.location.search));
  React.useEffect(() => {
    const restore = () => {
      if (window.location.pathname === '/events/') setFilter(readEventsAddress(window.location.search));
    };
    window.addEventListener('popstate', restore);
    return () => window.removeEventListener('popstate', restore);
  }, []);
  const [open, setOpen] = React.useState(false);
  const change = (patch) => {
    const next = { ...filter, ...patch };
    setFilter(next);
    const search = eventsAddress(next);
    window.history.replaceState(window.history.state, '', `${window.location.pathname}${search ? `?${search}` : ''}`);
  };
  const [listed, retry] = useEvents(eventsQuery(filter));
  const events = listed.kind === 'ready' ? listed.data.events : [];
  const places = listed.kind === 'ready' ? listed.data.places : [];
  const [one, retryOne] = useOne(route.view === 'event' ? route.event : null, events);
  const list = eventList(events, filter.when === 'past', today);
  const back = eventBack();
  const returnLink = <Box><Button href={back.href} onClick={back.onClick} startIcon={<ChevronLeftRounded />} color="inherit" sx={{ ml: -1 }}>
    {label(back.calendar ? 'cart.back' : 'events.back')}</Button></Box>;
  // A phone: an event is a page of its own.
  if (!wide && route.view === 'event') {
    let body = <Waiting />;
    if (one.kind === 'error') body = <Failed onRetry={retryOne} />;
    else if (one.kind === 'ready' && one.event) body = <EventDetail event={one.event} today={today} admin={config.admin} wide={false} />;
    else if (one.kind === 'ready') body = <Typography>{label('events.not-found')}</Typography>;
    return <Box sx={{ maxWidth: 720, mx: 'auto' }}><Stack spacing={1.5}>
      {returnLink}
      {body}
    </Stack></Box>;
  }
  const picked = one.kind === 'ready' ? one.event : null;
  const shown = picked || (wide && !route.event ? list.first : null);
  const chips = [];
  for (const key of filter.categories) {
    chips.push(<Chip key={key} size="small" label={categoryWord(key)} onClick={() => setOpen(true)} sx={chipX}
      onDelete={() => change({ categories: filter.categories.filter((one_) => one_ !== key) })}
      deleteIcon={<CloseRounded aria-label={label('filter.remove', { name: categoryWord(key) })} />} />);
  }
  if (filter.where !== 'all') {
    const name = placeName(filter.where, places) ?? filter.where;
    chips.push(<Chip key="where" size="small" label={name} onClick={() => setOpen(true)} sx={chipX} onDelete={() => change({ where: 'all' })}
      deleteIcon={<CloseRounded aria-label={label('filter.remove', { name })} />} />);
  }
  let body = null;
  if (listed.kind === 'loading') body = <Waiting />;
  else if (listed.kind === 'error') body = <Failed onRetry={retry} />;
  else {
    body = <>
      {list.weekly.length > 0 && <Section title={label('events.every-week')}>
        {list.weekly.map((row) => <EventRow key={row.event.id} row={row} chosen={wide && shown?.id === row.event.id} link={link} />)}
      </Section>}
      {list.months.map((month) => <Section key={month.month} title={month.title}>
        {month.rows.map((row) => <EventRow key={row.event.id} row={row} chosen={wide && shown?.id === row.event.id} link={link} />)}
      </Section>)}
      {list.count === 0 && <Typography sx={{ color: 'text.secondary' }}>{label('events.none')}</Typography>}
    </>;
  }
  const rows = <Stack spacing={2.5}>
    <Stack direction="row" sx={{ gap: 1, alignItems: 'center' }}>
      <Stack direction="row" sx={{ flex: 1, minWidth: 0, gap: 1, flexWrap: 'wrap', alignItems: 'center' }}>
        <Button variant="outlined" size="small" startIcon={<TuneRounded />} onClick={() => setOpen(true)}>{label('filter.button')}</Button>
        {chips}
      </Stack>
      <ToggleButtonGroup size="small" exclusive value={filter.when} aria-label={label('events.when')}
        onChange={(_, next) => next && change({ when: next })}>
        <ToggleButton value="coming" sx={{ px: 1.25, py: 0.25 }}>{label('events.coming')}</ToggleButton>
        <ToggleButton value="past" sx={{ px: 1.25, py: 0.25 }}>{label('events.past')}</ToggleButton>
      </ToggleButtonGroup>
    </Stack>
    {body}
  </Stack>;
  return <Stack spacing={2}>
    {route.view === 'event' && returnLink}
    <Typography component="h1" variant="h4">{label('title.events')}</Typography>
    {wide ? <Box sx={{ display: 'grid', gridTemplateColumns: 'minmax(0, 5fr) minmax(0, 7fr)', gap: 4, alignItems: 'start' }}>
      {rows}
      <Box sx={{ position: 'sticky', top: 16 }}>
        {shown && <EventDetail event={shown} today={today} admin={config.admin} wide />}
        {!shown && one.kind === 'error' && <Failed onRetry={retryOne} />}
        {!shown && one.kind === 'ready' && <Typography>{label('events.not-found')}</Typography>}
      </Box>
    </Box> : rows}
    <EventsFilter open={open} phone={!wide} filter={filter} places={places} onChange={change} onClose={() => setOpen(false)} />
  </Stack>;
}
