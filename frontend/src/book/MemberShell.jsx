// SPDX-License-Identifier: AGPL-3.0-only
import React from 'react';
import {
  Alert, BottomNavigation, BottomNavigationAction, Box, Button, CircularProgress,
  Link, Snackbar, Stack, ToggleButton, ToggleButtonGroup, Typography, useMediaQuery,
} from '@mui/material';
import { useTheme } from '@mui/material/styles';
import { CalendarBlank } from '@phosphor-icons/react/dist/csr/CalendarBlank';
import { Ticket } from '@phosphor-icons/react/dist/csr/Ticket';
import { User } from '@phosphor-icons/react/dist/csr/User';
import { BookShown } from './AddSheet';
import { readCart, readChoices, saveCategoryChoice } from './api';
import CartPill, { pillPlace } from './CartPill';
import { typeOrder } from './cart.js';
import { addressOf, filterOf, pickOf, readAddress } from './filter.js';
import { categoryTabId, CALENDAR_ID } from './categories.js';
import { BookingAs, FilterBar, FilterSheet } from './FilterControls';
import { pillStrip, SIDE_BY_SIDE } from './fit.js';
import { label } from './labels.js';
import BookMonth from './BookMonth';
import BookWeek from './BookWeek';
import CartPage from './CartPage';
import OutcomePage from './OutcomePage';
import MyPage from './MyPage';
import PricesPage from './PricesPage';
import { ConditionsText } from './PublicPage';
import EventsPage from './EventsPage';
import BookedSheet from './BookedSheet';
import BetaBanner from './BetaBanner';
import PageFooter from './PageFooter';
import {
  bookOpened, bookState, goTo, hrefFor, keepBookState, placeOfView, PLACES, placeWords, restoreScroll, staysInPage, stopScrollRestore, takeDay, trackScroll, viewOf, viewTitle,
} from './places.js';

// The places wear fine line icons (Phosphor, light), no disc: the chosen one in
// full white, the others the quiet grey. Icons are decoration; the words name them.
// Admin and Sign out are rows at the end of My page .
const GLYPHS = { book: CalendarBlank, events: Ticket, me: User };

// Sign out is the same POST form with the CSRF token as before; My page's row
// submits it by its id.
const SIGN_OUT = 'rr-sign-out';

function SignOutForm({ csrf }) {
  return <form id={SIGN_OUT} method="post" action="/logout/" hidden>
    <input type="hidden" name="csrfmiddlewaretoken" value={csrf} />
  </form>;
}

// The phone's bottom bar: the three places.
function Tabs({ tab, go }) {
  return <BottomNavigation showLabels value={tab}
    sx={{ position: 'fixed', left: 0, right: 0, bottom: 0, zIndex: 'appBar', height: 'auto',
      bgcolor: 'background.paper', borderTop: 1, borderColor: 'divider',
      pt: 0.75, pb: 'calc(6px + env(safe-area-inset-bottom))', pl: 'env(safe-area-inset-left)', pr: 'env(safe-area-inset-right)' }}>
    {PLACES.map(([key, href]) => {
      const Glyph = GLYPHS[key];
      return <BottomNavigationAction key={key} value={key} label={placeWords(key)}
        component="a" href={href} onClick={go(key)} aria-current={tab === key ? 'page' : undefined}
        icon={<Glyph size={26} weight="light" aria-hidden="true" />}
        sx={{ gap: 0.5, minWidth: 0, color: 'text.secondary', '&.Mui-selected': { color: 'text.primary', fontWeight: 600 },
          '& .MuiBottomNavigationAction-label.Mui-selected': { fontSize: '0.75rem' } }} />;
    })}
  </BottomNavigation>;
}

function HeaderPlaces({ tab, go }) {
  return <Stack direction="row" spacing={0.5} component="nav" sx={{ flexShrink: 0 }}>
    {PLACES.map(([key, href]) => {
      const Glyph = GLYPHS[key];
      return <Button key={key} href={href} onClick={go(key)} aria-current={tab === key ? 'page' : undefined}
        startIcon={<Glyph size={20} weight="light" aria-hidden="true" />}
        // A place's name never wraps; below 800 px the button is tighter.
        sx={{ color: tab === key ? 'text.primary' : 'text.secondary', fontWeight: tab === key ? 600 : 400, whiteSpace: 'nowrap',
          flexShrink: 0, '& .MuiButton-startIcon': { mr: { xs: 0.5, md: 1 } }, px: { xs: 1, md: 2 } }}>
        <Typography component="span" sx={{ font: 'inherit' }}>{placeWords(key)}</Typography>
      </Button>;
    })}
  </Stack>;
}

// The logo and the names are home, Book .
function Header({ config, tab, wide, go, home }) {
  return <Stack component="header" direction="row" spacing={1.5}
    sx={{ alignItems: 'center', px: 2, py: 1, borderBottom: 1, borderColor: 'divider' }}>
    <Stack component="a" {...home} direction="row" spacing={1.5}
      sx={{ flexGrow: 1, minWidth: 0, alignItems: 'center', color: 'inherit', textDecoration: 'none' }}>
      <Box component="img" src={config.logo} alt="" sx={{ width: { xs: 48, md: 64 }, height: { xs: 48, md: 64 }, flexShrink: 0 }} />
      <Box sx={{ minWidth: 0 }}>
        <Typography noWrap sx={{ fontWeight: 600 }}>{label('shell.product')}</Typography>
        <Typography variant="body2" title={config.organization} sx={{ color: 'text.secondary', display: '-webkit-box', WebkitBoxOrient: 'vertical',
          WebkitLineClamp: 2, overflow: 'hidden', lineHeight: 1.3 }}>{config.organization}</Typography>
      </Box>
    </Stack>
    {wide && <HeaderPlaces tab={tab} go={go} />}
  </Stack>;
}

const VIEWS = ['month', 'week'];
// The cart's, the page after paying's and My page's column on a wide screen.
const PAGE_WIDTH = 720;

// The address is the filter's link; replaceState keeps the history as it was.
function writeAddress(filter) {
  const search = addressOf(filter);
  window.history.replaceState(window.history.state, '', `${window.location.pathname}${search ? `?${search}` : ''}${window.location.hash}`);
}

// Book stays mounted while My page shows, so its month, Filter and anything it
// holds are as they were left. `active` is whether it shows; `visit` counts the
// returns to it, each a quiet refresh of the calendar. The page carries the
// member's choices (`config.choices`), so the calendar's question starts at once.
function Book({ config, wide, side, active, visit, cartLink, pricesLink, onPill = () => {} }) {
  const [saved] = React.useState(bookState);
  const [attempt, setAttempt] = React.useState(0);
  const [state, setState] = React.useState(() => (config.choices
    ? { kind: 'ready', choices: config.choices, pick: saved?.pick || readAddress(window.location.search) }
    : { kind: 'loading' }));
  const [open, setOpen] = React.useState(false);
  // The Month opens first; the choice stays while Book stays mounted.
  const [calendar, setCalendar] = React.useState(saved?.calendar || 'month');
  // The chosen day, shared by both views.
  const [chosen, setChosen] = React.useState(saved?.chosen || null);
  const [anchors, setAnchors] = React.useState(saved?.anchors || {});
  const monthWindow = React.useCallback((month) => setAnchors((was) => was.month === month ? was : { ...was, month }), []);
  const weekWindow = React.useCallback((week) => setAnchors((was) => was.week === week ? was : { ...was, week }), []);
  // A day asked from elsewhere (an event's "See it in Book"): the Week on it.
  const [jump, setJump] = React.useState(0);
  const currentBook = React.useRef(null);
  React.useEffect(() => {
    const restore = () => {
      if (window.location.pathname !== '/book/') return;
      const previous = bookState();
      if (!previous) return;
      // Keep a mounted selection sheet when this entry is still as it was
      // left (for example, returning from its price explanation).
      if (JSON.stringify(previous) === JSON.stringify(currentBook.current)) return;
      setCalendar(previous.calendar);
      setChosen(previous.chosen);
      setAnchors(previous.anchors);
      setState((was) => ({ ...was, pick: previous.pick }));
      setJump((count) => count + 1);
    };
    window.addEventListener('popstate', restore);
    return () => window.removeEventListener('popstate', restore);
  }, []);
  React.useEffect(() => {
    const take = () => {
      const day = takeDay();
      if (!day) return;
      setChosen(day);
      setCalendar('week');
      setAnchors((was) => ({ ...was, week: day }));
      setJump((count) => count + 1);
    };
    take();
    window.addEventListener('rr-show-day', take);
    return () => window.removeEventListener('rr-show-day', take);
  }, []);
  // The member's cart, for the pill; a failed read leaves it as it was.
  const [cart, setCart] = React.useState(null);
  // "Added to your cart", shown about 3 s after an add.
  const [notice, setNotice] = React.useState({ count: 0, open: false });
  // The pill's real height, so the notice sits just above it.
  const pill = React.useRef(null);
  const [pillHeight, setPillHeight] = React.useState(0);
  const [pillWidth, setPillWidth] = React.useState(0);
  const pillShows = Boolean(cart && cart.count > 0);
  // Read when the pill appears (the calendar's choices may load after the cart does) and whenever its size changes.
  React.useLayoutEffect(() => {
    const box = pill.current;
    if (!box) {
      setPillHeight(0);
      setPillWidth(0);
      return undefined;
    }
    const read = () => { setPillHeight(box.offsetHeight); setPillWidth(box.offsetWidth); };
    read();
    const watch = new ResizeObserver(read);
    watch.observe(box);
    return () => watch.disconnect();
  }, [pillShows, wide, state.kind]);
  // The shell lifts the phone footer over the pill only while the pill shows.
  React.useEffect(() => { onPill(pillHeight); }, [pillHeight, onPill]);
  const onAdded = (next) => { setCart(next); setNotice((was) => ({ count: was.count + 1, open: true })); };
  // After an add the sheet and the slot it came from are gone: the focus goes
  // to the cart pill.
  React.useEffect(() => {
    if (notice.count) pill.current?.querySelector('a')?.focus();
  }, [notice.count]);
  React.useEffect(() => {
    let live = true;
    readCart().then((result) => { if (live && result.ok) setCart(result.data); });
    return () => { live = false; };
  }, [visit]);
  React.useEffect(() => {
    if (attempt === 0 && config.choices) return undefined;
    let live = true;
    setState({ kind: 'loading' });
    readChoices().then((result) => {
      if (!live) return;
      if (result.kind === 'signed-out') window.location.assign('/login/');
      else if (result.ok) setState({ kind: 'ready', choices: result.data, pick: bookState()?.pick || readAddress(window.location.search) });
      else setState({ kind: result.kind });
    });
    return () => { live = false; };
  }, [attempt]);
  // The address carries the filter whenever Book shows: on load, so a link
  // copied before any change has the category, and on each return.
  const shown = state.kind === 'ready' ? filterOf(state.choices, state.pick) : null;
  currentBook.current = { calendar, chosen, anchors, pick: shown ? pickOf(shown) : state.pick };
  React.useEffect(() => {
    if (active && shown) writeAddress(shown);
  }, [active, state.kind]);
  React.useEffect(() => {
    if (active && shown) keepBookState(currentBook.current);
  }, [active, calendar, chosen, anchors, state]);
  if (state.kind === 'loading' || state.kind === 'signed-out') {
    return <Box sx={{ display: 'flex', justifyContent: 'center', py: 4 }}><CircularProgress aria-label={label('common.loading')} /></Box>;
  }
  if (state.kind === 'forbidden') {
    return <Stack spacing={1}><Typography>{label('common.no-access')}</Typography><Link href="/login/">{label('common.sign-in')}</Link></Stack>;
  }
  if (state.kind !== 'ready') {
    return <Alert severity="error" action={<Button color="inherit" size="small" onClick={() => setAttempt(attempt + 1)}>{label('common.try-again')}</Button>}>
      {label('common.could-not-load')}
    </Alert>;
  }
  const filter = filterOf(state.choices, state.pick);
  if (!filter) return <Typography>{label('common.nothing-to-book')}</Typography>;
  // A change is the new address; only the switch's own category change is kept
  // for the member, never one that came from a link or the empty day's button.
  const change = (patch, keepCategory) => {
    const pick = { ...pickOf(filter), ...patch };
    const next = filterOf(state.choices, pick);
    if (active) writeAddress(next);
    setState({ ...state, pick });
    if (keepCategory) saveCategoryChoice(config.csrf, next.category);
  };
  return <BookShown.Provider value={active}>
    <Typography component="h1" variant="h4">{filter.page.name}</Typography>
    {filter.page.description && <Typography sx={{ color: 'text.secondary' }}>{filter.page.description}</Typography>}
    <BookingAs filter={filter} wide={wide} onChange={change} />
    <Stack direction="row" sx={{ gap: 1, alignItems: 'center' }}>
      <Box sx={{ flex: 1, minWidth: 0 }}><FilterBar filter={filter} onOpen={() => setOpen(true)} onChange={change} /></Box>
      <ToggleButtonGroup size="small" exclusive value={calendar} aria-label={label('view.label')}
        onChange={(_, next) => { if (next) { setAnchors({}); setCalendar(next); } }}>
        {VIEWS.map((key) => <ToggleButton key={key} value={key} sx={{ px: 1.25, py: 0.25 }}>{label(`view.${key}`)}</ToggleButton>)}
      </ToggleButtonGroup>
    </Stack>
    <FilterSheet open={open} phone={!wide} filter={filter} onChange={change} onClose={() => setOpen(false)} />
    {/* With the switch shown, the calendar is the region its tabs control. */}
    <Box {...(filter.categories.length > 1
      ? { role: 'tabpanel', id: CALENDAR_ID, 'aria-labelledby': categoryTabId(filter.category) } : {})}>
    {calendar === 'week'
      ? <BookWeek key={jump} filter={filter} wide={wide} onChange={change} csrf={config.csrf} cart={cart} onCart={onAdded} refresh={visit} cartLink={cartLink} pricesLink={pricesLink}
        initialFrom={anchors.week} onWindow={weekWindow} chosen={chosen} onChoose={setChosen} order={typeOrder(state.choices)} below={wide ? pillStrip(pillHeight) : 0} />
      : <BookMonth key={jump} filter={filter} wide={wide} side={side} onChange={change} csrf={config.csrf} cart={cart} onCart={onAdded} refresh={visit} cartLink={cartLink} pricesLink={pricesLink}
        initialMonth={anchors.month} onWindow={monthWindow} chosen={chosen} onChoose={setChosen} order={typeOrder(state.choices)} />}
    </Box>
    <CartPill ref={pill} cart={cart} wide={wide} link={cartLink} />
    <BookedSheet wide={wide} contact={config.contact_email} />
    <Snackbar key={notice.count} open={notice.open} autoHideDuration={3000}
      onClose={(_, reason) => { if (reason !== 'clickaway') setNotice((was) => ({ ...was, open: false })); }}
      message={label('cart.added')} anchorOrigin={{ vertical: 'bottom', horizontal: 'right' }}
      // In the pill's strip, on the pill's left, on every width: it covers nothing the
      // member can press (the strip is kept free of content), and the next press is
      // often beside the last one. MUI's own place for a wide screen is set at its
      // breakpoint, so the offset is set there too.
      slotProps={{ content: { sx: { minWidth: 0, whiteSpace: 'nowrap', px: wide ? 2 : 1.5, py: wide ? 1 : 0.5 } } }}
      sx={(theme) => {
        const { right, bottom } = pillPlace(wide);
        const place = { right: `calc(${right} + ${pillWidth}px + ${wide ? 12 : 8}px)`, bottom, left: wide ? 'auto' : 'calc(12px + env(safe-area-inset-left))' };
        return { ...place, [theme.breakpoints.up('sm')]: place };
      }} />
  </BookShown.Provider>;
}

export default function MemberShell({ config }) {
  // Drawn in the browser only, so the first frame already knows the width.
  const wide = useMediaQuery(useTheme().breakpoints.up('sm'), { noSsr: true });
  // Beside its day panel the Month needs more room than the wide layout's first width (part 3.9984).
  const side = useMediaQuery(`(min-width:${SIDE_BY_SIDE}px)`, { noSsr: true });
  // The view: Book, My page, the cart or an order's outcome. The cart and the
  // outcome belong to Book, which stays the chosen place (part 3.7).
  const [route, setRoute] = React.useState(() => viewOf(window.location.pathname) || { view: config.screen, order: config.order });
  // The page the browser loaded: the price list and the booking conditions
  //  have addresses of their own, which the views do not name.
  const loaded = React.useRef(window.location.pathname);
  // The cart pill's height on Book, 0 while it is hidden (for the phone footer).
  const [pillRoom, setPillRoom] = React.useState(0);
  const [visit, setVisit] = React.useState(0);
  const [opened, setOpened] = React.useState(() => bookOpened(false, config.screen));
  const tab = placeOfView(route.view);
  const show = React.useCallback((next) => {
    setRoute(next);
    document.title = viewTitle(next.view);
    setOpened((was) => bookOpened(was, next.view));
    if (next.view === 'book') setVisit((count) => count + 1);
  }, []);
  // The browser's back and forward between the views.
  React.useEffect(() => {
    const moved = () => {
      const next = viewOf(window.location.pathname)
        || (window.location.pathname === loaded.current ? { view: config.screen, order: config.order } : null);
      if (next) show(next);
    };
    window.addEventListener('popstate', moved);
    return () => window.removeEventListener('popstate', moved);
  }, [show, config]);
  const open = (next) => {
    goTo(hrefFor(next));
  };
  React.useLayoutEffect(() => {
    const before = window.history.scrollRestoration;
    window.history.scrollRestoration = 'manual';
    const untrack = trackScroll();
    return () => { untrack(); window.history.scrollRestoration = before; };
  }, []);
  React.useLayoutEffect(() => {
    if (restoreScroll()) return undefined;
    // A reload may need calendar data before the saved position exists. Stop
    // once it fits, on another route, or when the member starts scrolling.
    const watch = new ResizeObserver(() => { if (restoreScroll()) stop(); });
    const stop = () => {
      stopScrollRestore();
      watch.disconnect();
      for (const event of ['wheel', 'touchstart', 'keydown', 'pointerdown']) window.removeEventListener(event, stop);
    };
    watch.observe(document.body);
    for (const event of ['wheel', 'touchstart', 'keydown', 'pointerdown']) window.addEventListener(event, stop, { passive: true });
    return stop;
  }, [route]);
  // A link to a view, kept in the page by a plain left click.
  const link = (next) => ({
    href: hrefFor(next),
    onClick: (event) => {
      if (!staysInPage(event, event.currentTarget)) return;
      event.preventDefault();
      if (next.view === route.view && next.order === route.order && next.event === route.event) return;
      open(next);
    },
  });
  const go = (place) => link({ view: place }).onClick;
  // A new view takes the focus to its heading, so a keyboard and a screen
  // reader start there, not on the link left behind.
  const content = React.useRef(null);
  const shown = React.useRef(route);
  React.useEffect(() => {
    if (shown.current === route) return undefined;
    shown.current = route;
    // Back on Book with a sheet left open: the sheet takes the focus.
    if (route.view === 'book' && document.querySelector('[role="dialog"]')) return undefined;
    const page = content.current;
    const heading = () => [...page.querySelectorAll('h1')].find((one) => !one.closest('[hidden]'));
    const take = (target) => {
      target.setAttribute('tabindex', '-1');
      target.focus({ preventScroll: true });
    };
    const found = heading();
    if (found) {
      take(found);
      return undefined;
    }
    // A view that loads first: its heading when it comes, unless the member
    // has moved on.
    take(page);
    const watch = new MutationObserver(() => {
      const later = heading();
      if (!later) return;
      watch.disconnect();
      if (document.activeElement === page) take(later);
    });
    watch.observe(page, { childList: true, subtree: true });
    return () => watch.disconnect();
  }, [route]);
  // The space kept under the footer: on a phone the bottom navigation, and on Book the pill above it;
  // on a wide screen the pill's strip on Book.
  const pillShows = route.view === 'book' && pillRoom > 0;
  const safe = 'env(safe-area-inset-bottom)';
  let footLift = 0;
  if (!wide) footLift = `calc(${pillShows ? 80 + pillRoom + 8 : 80}px + ${safe})`;
  else if (pillShows) footLift = `calc(${pillStrip(pillRoom)}px + ${safe})`;
  // As wide as the demo's (1240px), centred.
  return <Box sx={{ minHeight: '100dvh', display: 'flex', flexDirection: 'column' }}>
    <BetaBanner address={config.contact_email} />
    <Box sx={{ flex: 1, display: 'flex', flexDirection: 'column', width: '100%', maxWidth: 1240, mx: 'auto' }}>
    <SignOutForm csrf={config.csrf} />
    <Header config={config} tab={tab} wide={wide} go={go} home={link({ view: 'book' })} />
    <Box ref={content} sx={{
      flexGrow: 1, p: 2,
      '&:focus, & h1:focus': { outline: 'none' },
    }}>
      <Stack spacing={2} hidden={route.view !== 'book'} sx={{ display: route.view === 'book' ? 'flex' : 'none' }}>
        {bookOpened(opened, route.view) && <Book config={config} wide={wide} side={side} active={route.view === 'book'} visit={visit}
          cartLink={link({ view: 'cart' })} pricesLink={link({ view: 'prices' })} onPill={setPillRoom} />}
      </Stack>
      {/* Pages around the calendar use a readable column width. */}
      {placeOfView(route.view) === 'events' && <EventsPage config={config} route={route} link={link} wide={wide} />}
      {route.view !== 'book' && placeOfView(route.view) !== 'events' && <Box sx={{ maxWidth: PAGE_WIDTH, mx: 'auto' }}>
        {route.view === 'cart' && <CartPage config={config} link={link} onOutcome={(order) => open({ view: 'order', order })} />}
        {route.view === 'order' && <OutcomePage key={route.order} config={config} orderId={route.order} link={link} />}
        {route.view === 'me' && <Stack spacing={2}><MyPage config={config} link={link} /></Stack>}
        {route.view === 'prices' && <PricesPage config={config} link={link} />}
        {route.view === 'conditions' && <ConditionsText text={config.conditions} />}
      </Box>}
    </Box>
    {/* On a phone the footer clears the bottom navigation, and on Book the cart pill above it while it shows; on a wide screen it clears the pill's strip. */}
    <PageFooter config={config} lift={footLift} links />
    {!wide && <Tabs tab={tab} go={go} />}
    </Box>
  </Box>;
}
