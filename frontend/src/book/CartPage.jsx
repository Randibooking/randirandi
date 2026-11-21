// SPDX-License-Identifier: AGPL-3.0-only
import React from 'react';
import { Alert, Box, Button, Checkbox, CircularProgress, FormControlLabel, Link, Stack, Typography } from '@mui/material';
import ArrowBackRounded from '@mui/icons-material/ArrowBackRounded';
import { moneyWords } from './add.js';
import {
  afterCancel, afterOrder, afterPaymentStart, canPay, cartView, clockWords, groupLines, onlyLine, paidWithVipps, paymentUnavailable, payWords,
  releasedWords, removeLine, secondsLeft,
} from './cart.js';
import { cancelOrder, orderCart, readCart, removeFromCart, startPayment } from './api';
import { calendars } from './kept.js';
import { label, labelParts } from './labels.js';
import LineGroups from './LineGroups';
import { useTypeOrder } from './useTypeOrder.js';
import { sentence } from './words.js';

// The cart page and, while an order is held, the payment page, ported from the
// owner's demo (`CartPage`, `Countdown`, `PayPage`) to the real calls.
const signIn = () => window.location.assign('/login/');

// How it is paid: Vipps' own paymark, unchanged, and the payment type in words.
function PaidWith({ config }) {
  return <Stack direction="row" spacing={1} sx={{ alignItems: 'center', justifyContent: 'center', mb: 1.5 }}>
    <Box component="img" src={config.paymark} alt="" sx={{ height: 28, width: 'auto', display: 'block' }} />
    <Typography>{label('cart.paid-with')}</Typography>
  </Stack>;
}

function Countdown({ until, onEnd }) {
  const [now, setNow] = React.useState(Date.now);
  React.useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, []);
  const left = secondsLeft(until, now);
  React.useEffect(() => { if (left === 0) onEnd(); }, [left === 0]);
  return <b>{clockWords(left ?? 0)}</b>;
}

export default function CartPage({ config, link, onOutcome }) {
  const order = useTypeOrder(config);
  const [state, setState] = React.useState({ kind: 'loading' });
  const [notice, setNotice] = React.useState(null);
  const [busy, setBusy] = React.useState(false);
  const [tried, setTried] = React.useState(false);
  // The box above Pay : ticked for the conditions the cart showed, and
  // unticked again when they change.
  const [accepted, setAccepted] = React.useState(false);
  const version = state.kind === 'ready' ? state.cart.conditions?.version : null;
  React.useEffect(() => { setAccepted(false); }, [version]);
  // When Remove, Cancel payment or the hold running out takes the focused
  // element away, the focus goes to the heading once the page is drawn
  // again. `refocus` holds the element focused when it began; a focus that
  // moved on stays.
  const heading = React.useRef(null);
  const refocus = React.useRef(null);
  React.useEffect(() => {
    const from = refocus.current;
    if (!from || !heading.current) return;
    const at = document.activeElement;
    if (at === from && from !== document.body) return;
    refocus.current = null;
    if (at && at !== document.body) return;
    heading.current.setAttribute('tabindex', '-1');
    heading.current.focus({ preventScroll: true });
  });
  const focused = () => document.activeElement || document.body;
  const [attempt, setAttempt] = React.useState(0);

  // A read that fails leaves the cart as it was.
  const reread = React.useCallback(async (first = false) => {
    const result = await readCart();
    if (result.ok) setState({ kind: 'ready', cart: result.data });
    else if (result.kind === 'signed-out') signIn();
    else if (first || result.kind === 'forbidden') setState({ kind: result.kind });
    return result;
  }, []);
  React.useEffect(() => { reread(true); }, [attempt]);
  // Back from Vipps the browser may show this page from its cache: read again.
  React.useEffect(() => {
    const shown = (event) => { if (event.persisted) { setBusy(false); reread(); } };
    window.addEventListener('pageshow', shown);
    return () => window.removeEventListener('pageshow', shown);
  }, [reread]);

  if (state.kind === 'loading') {
    return <Box sx={{ display: 'flex', justifyContent: 'center', py: 4 }}><CircularProgress aria-label={label('common.loading')} /></Box>;
  }
  if (state.kind === 'forbidden') {
    return <Stack spacing={1}><Typography>{label('common.no-access')}</Typography><Link href="/login/">{label('common.sign-in')}</Link></Stack>;
  }
  if (state.kind !== 'ready') {
    return <Alert severity="error" action={<Button color="inherit" size="small" onClick={() => { setState({ kind: 'loading' }); setAttempt(attempt + 1); }}>{label('common.try-again')}</Button>}>
      {label('common.could-not-load')}
    </Alert>;
  }
  const { cart } = state;
  const view = cartView(cart);
  const groups = groupLines(view.shown, order);
  const single = onlyLine(groups);
  const money = (amount) => (amount ? moneyWords(amount, cart.currency) : label('cart.free-of-charge'));

  const afterStart = (done) => {
    if (done.kind === 'done') { calendars.clear(); onOutcome(done.order); return; }
    if (done.kind === 'vipps') { calendars.clear(); window.location.assign(done.address); return; }
    setBusy(false);
    if (done.kind === 'signed-out') signIn();
    else if (done.kind === 'retry') { calendars.clear(); setTried(true); setNotice(paymentUnavailable()); reread(); }
    else if (done.kind === 'gone') { calendars.clear(); setTried(false); reread(); }
    else if (done.kind === 'refused') { setNotice(done.text); reread(); }
    else setNotice(label('cart.could-not-order'));
  };
  const pay = async () => {
    setBusy(true);
    setNotice(null);
    afterStart(afterOrder(await orderCart(config.csrf, cart.total, view.version)));
  };
  // The held order's payment again.
  const again = async () => {
    setBusy(true);
    setNotice(null);
    afterStart(afterPaymentStart(await startPayment(config.csrf, view.held.id), view.held.id));
  };
  // Let go of the held order: its times are back in the cart.
  const cancel = async () => {
    const from = focused();
    setBusy(true);
    setNotice(null);
    const done = afterCancel(await cancelOrder(config.csrf, view.held.id));
    setBusy(false);
    refocus.current = done.kind !== 'paid' && done.kind !== 'signed-out' ? from : null;
    if (done.kind === 'cart') { calendars.clear(); setTried(false); setState({ kind: 'ready', cart: done.cart }); }
    else if (done.kind === 'paid') { calendars.clear(); onOutcome(view.held.id); }
    else if (done.kind === 'gone') { calendars.clear(); reread(); }
    else if (done.kind === 'signed-out') signIn();
    else setNotice(done.text || label('cart.could-not-cancel'));
  };
  const remove = async (line) => {
    const from = focused();
    setBusy(true);
    setNotice(null);
    const done = await removeLine({ csrf: config.csrf, id: line.id, remove: removeFromCart, store: calendars });
    setBusy(false);
    if (done.kind === 'removed') setState({ kind: 'ready', cart: done.cart });
    else if (done.kind === 'signed-out') signIn();
    else { setNotice(done.text || label('cart.could-not-remove')); reread(); }
    refocus.current = done.kind !== 'signed-out' ? from : null;
  };
  const released = () => { calendars.clear(); setNotice(releasedWords()); refocus.current = focused(); reread(); };

  const back = <Button startIcon={<ArrowBackRounded />} {...link({ view: 'book' })} sx={{ mb: 1, ml: -1 }}>
    {label('cart.back')}</Button>;

  if (view.held) {
    return <Box>
      {back}
      <Typography ref={heading} component="h1" variant="h4" sx={{ mb: 0.5 }}>{label('cart.payment')}</Typography>
      <Typography variant="body2" sx={{ color: 'text.secondary', mb: 2 }}>
        {labelParts('cart.held', { reference: view.held.reference, time: <Countdown until={view.held.hold_expires_at} onEnd={released} /> })
          .map((part, index) => <React.Fragment key={index}>{part}</React.Fragment>)}
      </Typography>
      {notice && <Alert severity="warning" sx={{ mb: 2 }}>{sentence(notice)}</Alert>}
      <LineGroups groups={groups} currency={cart.currency} />
      {single ? <Box sx={{ my: 2 }} /> : <Stack direction="row" sx={{ justifyContent: 'space-between', my: 2 }}>
        <Typography variant="h6">{label('cart.total')}</Typography><Typography variant="h6">{money(view.held.total)}</Typography>
      </Stack>}
      {paidWithVipps(view.held.total) && <PaidWith config={config} />}
      <Button variant="contained" size="large" fullWidth disabled={busy} onClick={again}>
        {tried ? label('common.try-again') : label('cart.continue')}</Button>
      <Button size="large" fullWidth disabled={busy} onClick={cancel} sx={{ mt: 1 }}>{label('cart.cancel-payment')}</Button>
    </Box>;
  }
  return <Box>
    {back}
    <Typography ref={heading} component="h1" variant="h4" sx={{ mb: 1 }}>{label('cart.title')}</Typography>
    {notice && <Alert severity="info" sx={{ mb: 2 }}>{sentence(notice)}</Alert>}
    {!groups.length ? <Typography sx={{ color: 'text.secondary' }}>{label('cart.empty')}</Typography> : <>
      <LineGroups groups={groups} currency={cart.currency} onRemove={remove} busy={busy} />
      {single ? <Box sx={{ my: 2 }} /> : <Stack direction="row" sx={{ justifyContent: 'space-between', my: 2 }}>
        <Typography variant="h6">{label('cart.total')}</Typography>
        <Typography variant="h6">{money(view.total)}</Typography>
      </Stack>}
      {paidWithVipps(view.total) && <PaidWith config={config} />}
      <FormControlLabel sx={{ display: 'flex', alignItems: 'flex-start', mx: 0, mb: 1 }}
        control={<Checkbox checked={accepted} disabled={busy} onChange={(event) => setAccepted(event.target.checked)} sx={{ pt: 0.5 }} />}
        label={<Typography>{labelParts('cart.accept', {
          conditions: <Link href="/conditions/" target="_blank" rel="noopener noreferrer">{label('cart.accept-conditions')}</Link>,
          prices: <Link href="/prices/" target="_blank" rel="noopener noreferrer">{label('cart.accept-prices')}</Link>,
        }).map((part, index) => <React.Fragment key={index}>{part}</React.Fragment>)}</Typography>} />
      <Button variant="contained" size="large" fullWidth disabled={busy || !canPay(view, accepted)} onClick={pay}>
        {payWords(view.total, cart.currency)}</Button>
    </>}
  </Box>;
}
