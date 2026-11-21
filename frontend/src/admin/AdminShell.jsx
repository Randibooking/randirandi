// SPDX-License-Identifier: AGPL-3.0-only
import React from 'react';
import {
  Avatar, Box, Breadcrumbs, Button, Divider, Drawer, IconButton, Link, List, ListItemButton, ListItemIcon,
  ListItemText, ListSubheader, Stack, Typography, useMediaQuery,
} from '@mui/material';
import { useTheme } from '@mui/material/styles';
import ArrowBackRounded from '@mui/icons-material/ArrowBackRounded';
import BadgeRounded from '@mui/icons-material/BadgeRounded';
import BlockRounded from '@mui/icons-material/BlockRounded';
import EventRounded from '@mui/icons-material/EventRounded';
import BusinessRounded from '@mui/icons-material/BusinessRounded';
import CalendarViewDayRounded from '@mui/icons-material/CalendarViewDayRounded';
import CategoryRounded from '@mui/icons-material/CategoryRounded';
import ChevronRightRounded from '@mui/icons-material/ChevronRightRounded';
import CloseRounded from '@mui/icons-material/CloseRounded';
import HistoryRounded from '@mui/icons-material/HistoryRounded';
import MeetingRoomRounded from '@mui/icons-material/MeetingRoomRounded';
import MenuRounded from '@mui/icons-material/MenuRounded';
import PaymentsRounded from '@mui/icons-material/PaymentsRounded';
import PeopleAltRounded from '@mui/icons-material/PeopleAltRounded';
import ReceiptLongRounded from '@mui/icons-material/ReceiptLongRounded';
import SellRounded from '@mui/icons-material/SellRounded';
import ViewWeekRounded from '@mui/icons-material/ViewWeekRounded';
import Assets from './AssetPages';
import AuditLog from './AuditLog';
import BookingPage, { useBooking } from './BookingPage';
import BookingList from './BookingList';
import Blocks from './Blocks';
import ContainerPages from './ContainerPages';
import Events from './EventPages';
import ImportPage, { useImportRun } from './ImportPages';
import MemberPage, { MembersPlace, useMember } from './MemberPages';
import OrganizationPage from './OrganizationPage';
import PayoutPage, { PaymentsPlace, usePayout } from './PayoutPages';
import PricePages from './PricePages';
import RolePages from './RolePages';
import BetaBanner from '../book/BetaBanner';
import PageFooter from '../book/PageFooter';
import Schedule from './Schedule';
import TypePages from './TypePages';
import { crumbTrail } from './crumbs';
import { auditStep } from './auditLinks';
import { IMPORTS_ADDRESS, PAYOUTS_ADDRESS, bookingAddress, importsRoute, memberAddress, payoutsRoute, routeOf } from './bookings';
import { bookingStep, importsStep, memberStep, payoutStep, payoutsStep, placeStep, plainClick, runStep } from './route';
import { BACK_TO_BOOKING, BOOKING_HREF, GROUPS, hrefOf, initials, labelOf, titleOf } from './places';

// Menu, Who, Brand and the wide and phone layouts; the places are links and the
// menu panel opens in the browser only.
const ICONS = {
  bookings: ReceiptLongRounded, schedule: CalendarViewDayRounded, blocks: BlockRounded, events: EventRounded,
  members: PeopleAltRounded, payments: PaymentsRounded, assets: MeetingRoomRounded,
  containers: ViewWeekRounded, types: CategoryRounded, roles: BadgeRounded,
  prices: SellRounded, organization: BusinessRounded, audit: HistoryRounded,
};

// The places that are filled; the others stay empty until their parts. Each is
// drawn again only when its own props change.
const LISTS = {
  bookings: React.memo(BookingList), schedule: React.memo(Schedule), blocks: React.memo(Blocks), events: React.memo(Events),
  members: React.memo(MembersPlace), payments: React.memo(PaymentsPlace), audit: React.memo(AuditLog),
  types: React.memo(TypePages), roles: React.memo(RolePages), assets: React.memo(Assets),
  prices: React.memo(PricePages), containers: React.memo(ContainerPages), organization: React.memo(OrganizationPage),
};

function Menu({ place, go }) {
  return <List dense component="nav" aria-label="Admin" sx={{ p: 1 }}>
    {GROUPS.map(([group, items]) => <React.Fragment key={group}>
      <ListSubheader sx={{ bgcolor: 'transparent', lineHeight: '32px', mt: 0.5, fontSize: 12, color: 'text.secondary' }}>{group}</ListSubheader>
      {items.map(([key, label]) => {
        const Icon = ICONS[key];
        return <ListItemButton key={key} component="a" href={hrefOf(key)} selected={place === key}
          onClick={(event) => { if (plainClick(event)) { event.preventDefault(); go(key); } }}
          aria-current={place === key ? 'page' : undefined} sx={{ borderRadius: 1, mb: 0.25, '&.Mui-selected, &.Mui-selected:hover': { bgcolor: '#3b3a37', color: '#eeeeec', fontWeight: 600, boxShadow: `inset 3px 0 #eeeeec` } }}>
          <ListItemIcon sx={{ minWidth: 34 }}><Icon fontSize="small" /></ListItemIcon>
          <ListItemText primary={label} />
        </ListItemButton>;
      })}
    </React.Fragment>)}
  </List>;
}

// Under the name only Sign out: Back to Booking is the way to the member
// pages .
function Who({ config }) {
  return <Stack direction="row" spacing={1.25} sx={{ alignItems: 'center', p: 2, borderTop: 1, borderColor: 'divider' }}>
    <Avatar sx={{ width: 32, height: 32, fontSize: 14, bgcolor: '#f5f7fa', color: '#102f4c', border: '2px solid #102f4c' }}>{initials(config.name)}</Avatar>
    <Box sx={{ minWidth: 0, flex: 1 }}>
      <Typography variant="body2" noWrap sx={{ fontWeight: 600 }}>{config.name}</Typography>
      <Box component="form" method="post" action="/logout/" sx={{ display: 'flex' }}>
        <input type="hidden" name="csrfmiddlewaretoken" value={config.csrf} />
        <Link component="button" type="submit" variant="caption" underline="hover">Sign out</Link>
      </Box>
    </Box>
  </Stack>;
}

// The way back to the booking pages, easy to find but quiet: an outlined
// button, full width in the side menu, in the phone's top bar (7.111 asked
// "much bigger"; "somewhat smaller and more elegant").
function BackToBooking({ phone }) {
  return <Button component="a" href={BOOKING_HREF} variant="outlined" color="inherit" startIcon={<ArrowBackRounded />}
    fullWidth={!phone} size={phone ? 'small' : 'medium'}
    sx={phone ? { flexShrink: 0, whiteSpace: 'nowrap', px: 1.25 } : { justifyContent: 'flex-start' }}>
    {BACK_TO_BOOKING}</Button>;
}

// The logo and the name are Admin's home, the Bookings .
function home(go) {
  return { href: hrefOf('bookings'), onClick: (event) => { if (plainClick(event)) { event.preventDefault(); go('bookings'); } } };
}

function Brand({ config, go }) {
  return <Stack component="a" {...home(go)} direction="row" spacing={1.25}
    sx={{ alignItems: 'center', px: 2, py: 1.75, color: 'inherit', textDecoration: 'none' }}>
    <Box component="img" src={config.logo} alt="" sx={{ width: 36, height: 36 }} />
    <Box sx={{ minWidth: 0 }}>
      <Typography variant="body2" sx={{ fontWeight: 600 }}>RandiRandi admin</Typography>
      <Typography variant="caption" sx={{ color: 'text.secondary', display: 'block', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
        {config.organization}</Typography>
    </Box>
  </Stack>;
}

// The Bookings, the Schedule, the Blocks, the Payments and a member's page open a
// booking at its own address (/admin/bookings/<number>/), and the Members a
// member (/admin/members/<id>/), without leaving the page, so back and forward
// work and the list keeps its filters. An address opened on its own, or the
// back button after a reload, goes to the list by a page load.
function useRoute(config) {
  const [route, setRoute] = React.useState(() => {
    if (config.booking) return routeOf(bookingAddress(config.booking), window.history.state);
    if (config.member) return routeOf(memberAddress(config.member), window.history.state);
    if (config.imports) return importsRoute(config.run ?? null);
    if (config.payouts) return payoutsRoute(config.payout ?? null);
    return { place: config.place, booking: null, member: null };
  });
  React.useEffect(() => {
    const moved = () => setRoute(routeOf(window.location.pathname, window.history.state));
    window.addEventListener('popstate', moved);
    return () => window.removeEventListener('popstate', moved);
  }, []);
  // Stable, so a list that is not changing is not drawn again by the shell.
  const step = React.useCallback((next) => {
    window.history.pushState(next.state, '', next.address);
    setRoute(next.route);
    window.scrollTo(0, 0);
  }, []);
  const open = React.useCallback((number, from) => step(bookingStep(number, from)), [step]);
  const openMember = React.useCallback((id) => step(memberStep(id)), [step]);
  // A run of the imports, and a tab of the Members. The tab reads the route as
  // it is when clicked, so the callback stays the same between drawings.
  const latest = React.useRef(route);
  latest.current = route;
  const openRun = React.useCallback((number) => step(runStep(number)), [step]);
  const openPayout = React.useCallback((id) => step(payoutStep(id)), [step]);
  const openTab = React.useCallback((tab) => {
    const next = tab === 'imports' ? importsStep(latest.current) : placeStep(latest.current, 'members');
    if (next) step(next);
  }, [step]);
  const openPayTab = React.useCallback((tab) => {
    const next = tab === 'payouts' ? payoutsStep(latest.current) : placeStep(latest.current, 'payments');
    if (next) step(next);
  }, [step]);
  // A place of the menu: its list, by `pushState`, so back and forward work.
  const go = (place) => {
    const next = placeStep(route, place);
    if (next) step(next);
  };
  const close = () => {
    if (window.history.state?.listed) window.history.back();
    else window.location.assign(route.run ? IMPORTS_ADDRESS : route.payout ? PAYOUTS_ADDRESS : hrefOf(route.place));
  };
  return { route, open, openMember, openRun, openTab, openPayout, openPayTab, close, go };
}

export default function AdminShell({ config }) {
  // Drawn in the browser only, so the first frame already knows the width.
  const wide = useMediaQuery(useTheme().breakpoints.up('md'), { noSsr: true });
  const [menu, setMenu] = React.useState(false);
  // A price page's link to its container: the Containers place with it open.
  const [focus, setFocus] = React.useState(null);
  const toContainer = (id) => { setFocus({ id }); go('containers'); };
  // An audit row's link to an event: the Events place with it open.
  const [eventFocus, setEventFocus] = React.useState(null);
  const toEvent = (id) => { setEventFocus({ id }); go('events'); };
  const { route, open, openMember, openRun, openTab, openPayout, openPayTab, close, go } = useRoute(config);
  // What a place with pages under it has open (its breadcrumb), by place.
  const [crumbs, setCrumbs] = React.useState({});
  const crumbSetters = React.useMemo(() => Object.fromEntries(Object.keys(LISTS).map((key) => [key,
    (value) => setCrumbs((all) => (all[key] === value || (!all[key] && !value) ? all : { ...all, [key]: value }))])), []);
  // A place of the menu, or a link to one: its list, whatever page of it was open.
  const pick = (key) => { go(key); crumbs[key]?.go(0); };
  const toTarget = (what) => {
    const move = auditStep(what);
    if (move?.type === 'booking') open(move.number, 'audit');
    else if (move?.type === 'member') openMember(move.id);
    else if (move?.type === 'run') openRun(move.id);
    else if (move?.type === 'container') toContainer(move.id);
    else if (move?.type === 'event') toEvent(move.id);
    else if (move?.type === 'place') pick(move.place);
  };
  const place = route.place;
  const lookup = useBooking(route.booking);
  const person = useMember(route.member);
  const importRun = useImportRun(route.run ?? null);
  const payoutRead = usePayout(route.payout ?? null);
  const title = labelOf(place);
  const onList = route.booking === null && route.member === null && !route.run && !route.payout;
  const tab = route.imports ? 'imports' : 'members';
  const payTab = route.payouts ? 'payouts' : 'payments';
  // A place with a page open under its list says so (breadcrumb, no title); a
  // booking opened from such a place keeps that page in its breadcrumb.
  const crumb = (onList || route.booking !== null ? crumbs[place] : null) ?? null;
  const sub = onList && crumb !== null;
  const trail = crumbTrail({ onList, title, labels: crumb ? crumb.labels : [], booking: route.booking !== null, run: !!route.run, payout: !!route.payout })
    .map((item) => ({ ...item, go: item.action.type === 'crumb' ? () => crumb.go(item.action.index) : item.action.type === 'pick' ? () => pick(place) : close }));
  // A run just started is shown from the answer to its start.
  const started = React.useCallback((found) => { importRun.seed(found); openRun(found.number); }, [importRun.seed, openRun]);
  // A list, once shown, stays drawn while a booking or a member is open, for its filters.
  const shown = React.useRef({});
  if (onList) shown.current[place] = true;
  // A list shown again, after another place or a booking, asks again in the
  // background; what it drew stays on screen until the answer comes.
  const [again, setAgain] = React.useState({});
  const drawn = React.useRef({});
  const showing = onList ? place : null;
  React.useEffect(() => {
    if (showing === null) return;
    if (drawn.current[showing]) setAgain((a) => ({ ...a, [showing]: (a[showing] ?? 0) + 1 }));
    drawn.current[showing] = true;
  }, [showing]);
  React.useEffect(() => { document.title = titleOf(place); }, [place]);
  const openers = React.useMemo(() => ({
    bookings: (number) => open(number, 'bookings'), schedule: (number) => open(number, 'schedule'),
    blocks: (number) => open(number, 'blocks'), members: openMember, payments: (number) => open(number, 'payments'),
    events: (number) => open(number, 'events'), containers: (number) => open(number, 'containers'), audit: (number) => open(number, 'audit'),
  }), [open, openMember]);
  // The places that are filled; the others stay empty until their parts. The
  // Members' list opens a member, the others a booking.
  const page = <>
    {Object.entries(LISTS).map(([key, List]) => shown.current[key] && <Box key={key} sx={{ display: onList && place === key ? 'block' : 'none' }}>
      <List config={config} phone={!wide} again={again[key] ?? 0} open={openers[key]} focus={focus} eventFocus={eventFocus} toContainer={toContainer}
        setCrumb={crumbSetters[key]} openWhat={toTarget}
        {...(key === 'members' ? { tab, openTab, openRun, onStarted: started } : {})}
        {...(key === 'payments' ? { tab: payTab, openTab: openPayTab, openPayout } : {})} />
    </Box>)}
    {route.booking !== null && <BookingPage number={route.booking} state={lookup} phone={!wide} back={close} backLabel={title} csrf={config.csrf}
      openMember={openMember} />}
    {route.payout && <PayoutPage id={route.payout} state={payoutRead} phone={!wide} back={close}
      openBooking={(number) => open(number, 'payments')} />}
    {route.run && <ImportPage number={route.run} state={importRun} phone={!wide} back={close}
      openMember={openMember} openBooking={(number) => open(number, 'members')} />}
    {route.member !== null && <MemberPage key={route.member} id={route.member} state={person} phone={!wide} back={close} csrf={config.csrf}
      openBooking={(number) => open(number, 'members')} />}
  </>;
  if (!wide) {
    return <Box sx={{ minHeight: '100dvh', display: 'flex', flexDirection: 'column', position: 'relative' }}>
      <BetaBanner address={config.contact_email} />
      <Stack component="header" direction="row" spacing={1} sx={{ alignItems: 'center', px: 1.5, py: 1, borderBottom: 1, borderColor: 'divider', bgcolor: 'background.paper' }}>
        <IconButton aria-label="Menu" onClick={() => setMenu(true)}><MenuRounded /></IconButton>
        <Typography component={onList && !sub ? 'h1' : 'div'} noWrap sx={{ fontWeight: 600, flex: 1, minWidth: 0 }}>{title}</Typography>
        <BackToBooking phone />
        {/* The logo gives way below 360 px: the way back and the title come first. */}
        <Box component="a" {...home(pick)} aria-label={labelOf('bookings')}
          sx={{ display: 'flex', flexShrink: 0, '@media (max-width:359.95px)': { display: 'none' } }}>
          <Box component="img" src={config.logo} alt="" sx={{ width: 32, height: 32 }} />
        </Box>
      </Stack>
      <Box sx={{ flex: 1, minHeight: 0, p: 2 }}>{page}</Box>
      <PageFooter config={config} />
      {/* MUI's Drawer: focus moves in, Escape closes, the page behind is inert. */}
      <Drawer open={menu} onClose={() => setMenu(false)}
        slotProps={{ paper: { role: 'dialog', 'aria-label': 'Menu', sx: { width: '82%', bgcolor: 'background.paper',
          backgroundImage: 'none', display: 'flex', flexDirection: 'column', pl: 'env(safe-area-inset-left)' } } }}>
        <Stack direction="row" sx={{ alignItems: 'center', pr: 1 }}>
          <Box sx={{ flex: 1 }}><Brand config={config} go={(key) => { pick(key); setMenu(false); }} /></Box>
          <IconButton aria-label="Close" onClick={() => setMenu(false)}><CloseRounded /></IconButton>
        </Stack>
        <Divider />
        <Box sx={{ flex: 1, overflowY: 'auto' }}><Menu place={place} go={(key) => { pick(key); setMenu(false); }} /></Box>
        <Who config={config} />
      </Drawer>
    </Box>;
  }
  return <Box sx={{ minHeight: '100dvh', display: 'flex', flexDirection: 'column' }}>
    <BetaBanner address={config.contact_email} />
    <Box sx={{ display: 'flex', flex: 1 }}>
    <Box sx={{ width: 248, flexShrink: 0, borderRight: 1, borderColor: 'divider', bgcolor: 'background.paper', display: 'flex', flexDirection: 'column',
      position: 'sticky', top: 0, alignSelf: 'flex-start', height: '100dvh' }}>
      <Brand config={config} go={pick} />
      <Box sx={{ px: 2, pb: 1.5 }}><BackToBooking /></Box>
      <Divider />
      <Box sx={{ flex: 1, overflowY: 'auto' }}><Menu place={place} go={pick} /></Box>
      <Who config={config} />
    </Box>
    <Box sx={{ flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column' }}>
    <Box sx={{ flex: 1, p: 3 }}>
      <Stack spacing={0.5} sx={{ mb: 2.5 }}>
        <Breadcrumbs separator={<ChevronRightRounded fontSize="small" />} sx={{ '& .MuiBreadcrumbs-separator': { mx: 0.25 } }}>
          <Typography variant="body2" sx={{ color: 'text.secondary' }}>Admin</Typography>
          {trail.map((item) => (item.last
            ? <Typography key={item.index} variant="body2" sx={{ fontWeight: 600 }}>{item.label}</Typography>
            : <Link key={item.index} href={item.href ?? hrefOf(place)} variant="body2" underline="hover" color="text.secondary"
              onClick={(e) => { e.preventDefault(); item.go(); }}>{item.label}</Link>))}
          {route.run && <Typography variant="body2" sx={{ fontWeight: 600 }}>{`Import ${route.run}`}</Typography>}
          {route.payout && <Typography variant="body2" sx={{ fontWeight: 600 }}>{payoutRead.kind === 'found' && payoutRead.payout.id === route.payout ? `Payout ${payoutRead.payout.day}` : 'Payout'}</Typography>}
          {route.booking !== null && <Typography variant="body2" sx={{ fontWeight: 600 }}>{lookup.kind === 'found' && lookup.booking.number === route.booking ? `Reference ${lookup.booking.reference}` : 'Booking'}</Typography>}
          {route.member !== null && <Typography variant="body2" sx={{ fontWeight: 600 }}>{person.kind === 'found' && person.member.id === route.member && person.member.name ? person.member.name : `Member ${route.member}`}</Typography>}
        </Breadcrumbs>
        {onList && !sub && <Typography component="h1" variant="h4" sx={{ fontWeight: 600 }}>{title}</Typography>}
      </Stack>
      {page}
    </Box>
    <PageFooter config={config} />
    </Box>
    </Box>
  </Box>;
}
