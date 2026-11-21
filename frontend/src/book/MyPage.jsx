// SPDX-License-Identifier: AGPL-3.0-only
import React from 'react';
import { Alert, Avatar, Box, Button, ButtonBase, CircularProgress, Divider, Link, Stack, Typography } from '@mui/material';
import { CaretRight } from '@phosphor-icons/react/dist/csr/CaretRight';
import BedtimeOutlined from '@mui/icons-material/BedtimeOutlined';
import WbSunnyOutlined from '@mui/icons-material/WbSunnyOutlined';
import { brand, gray, orange } from '../theme/themePrimitives';
import { readMyBookings } from './api';
import { label } from './labels.js';
import { accountRows, booksAs, cancelMail, initialsOf, mineView, readMine, whoRows } from './mine.js';
import { useChoices } from './useChoices.js';

const NAVY = '#102f4c';

function List({ lines, link, ask }) {
  return <Box sx={{ border: 1, borderColor: 'divider', borderRadius: 3, px: 2, bgcolor: 'background.paper' }}>
    {lines.map((line, index) => <React.Fragment key={line.key}>
      {index > 0 && <Divider />}
      <Stack direction="row" spacing={1.5} sx={{ py: 1.25, alignItems: 'flex-start' }}>
        {line.night ? <BedtimeOutlined sx={{ fontSize: 20, mt: 0.25, color: brand[200] }} />
          : <WbSunnyOutlined sx={{ fontSize: 20, mt: 0.25, color: orange[300] }} />}
        <Box sx={{ flex: 1, minWidth: 0 }}>
          <Typography sx={{ fontWeight: 600 }}>{line.type}</Typography>
          <Typography variant="body2" sx={{ color: 'text.secondary' }}>{line.time}</Typography>
          <Typography variant="caption" sx={{ color: 'text.secondary', display: 'block' }}>
            {[line.asset, line.bookedAs, line.places].filter(Boolean).join(' · ')}</Typography>
          <Typography variant="caption" sx={{ display: 'block' }}>
            <Link {...link({ view: 'order', order: line.orderId })} color="inherit">{line.orderWords}</Link></Typography>
          {line.conditionsHref
            ? <Typography variant="caption" sx={{ display: 'block' }}><Link href={line.conditionsHref} color="inherit">{label('me.conditions')}</Link></Typography>
            : <Typography variant="caption" sx={{ color: 'text.secondary', display: 'block' }}>{label('me.conditions-none')}</Typography>}
          {line.cancelled && <Typography variant="caption" sx={{ color: 'error.light', display: 'block' }}>{line.cancelled}</Typography>}
          {ask(line) && <Button size="small" variant="outlined" color="inherit" href={ask(line)} sx={{ mt: 1 }}>{label('me.ask-cancel')}</Button>}
        </Box>
        {/* A cancelled booking's price is struck through: it is not what the member has. */}
        <Typography sx={{ fontWeight: 600, whiteSpace: 'nowrap',
          ...(line.cancelled && { textDecoration: 'line-through', color: 'text.secondary' }) }}>{line.price}</Typography>
      </Stack>
    </React.Fragment>)}
  </Box>;
}

// Each date once, its lines under it.
function Days({ days, link, ask }) {
  return <Stack spacing={2}>
    {days.map((group) => <Box key={group.day}>
      <Typography component="h3" variant="subtitle2" sx={{ mb: 0.5 }}>{group.date}</Typography>
      <List lines={group.lines} link={link} ask={ask} />
    </Box>)}
  </Stack>;
}

// The end of the page: Admin for an administrator, then Sign out (the shell's
// form, by its id).
function AccountRows({ admin }) {
  const row = { width: '100%', justifyContent: 'space-between', px: 2, py: 1.5, font: 'inherit', textAlign: 'left', color: 'text.secondary',
    '&.Mui-focusVisible, &:focus-visible': { outlineStyle: 'solid', outlineWidth: '2px', outlineColor: 'text.primary', outlineOffset: '-2px' } };
  const rows = accountRows(admin);
  return <Box sx={{ mt: 3, border: 1, borderColor: 'divider', borderRadius: 3, bgcolor: 'background.paper', overflow: 'hidden' }}>
    {rows.map((one, index) => <React.Fragment key={one.key}>
      {index > 0 && <Divider />}
      {one.href
        ? <ButtonBase component="a" href={one.href} sx={row}>{label('place.admin')}<CaretRight size={18} weight="light" aria-hidden="true" /></ButtonBase>
        : <ButtonBase component="button" type="submit" form="rr-sign-out" sx={row}>{label('me.sign-out')}</ButtonBase>}
    </React.Fragment>)}
  </Box>;
}

// My page: who the member is, their bookings (coming first, then earlier; each
// coming one with an email to ask for its cancellation) and the links the page
// had before. Admin and Sign out end it. Read each time it is shown.
export default function MyPage({ config, link }) {
  const [state, setState] = React.useState({ kind: 'loading' });
  const [attempt, setAttempt] = React.useState(0);
  const choices = useChoices(config);
  const books = booksAs(choices);
  const who = whoRows(config);
  React.useEffect(() => {
    let live = true;
    setState({ kind: 'loading' });
    readMyBookings().then((result) => {
      if (!live) return;
      const read = readMine(result);
      if (read.kind === 'signed-out') window.location.assign('/login/');
      else setState(read);
    });
    return () => { live = false; };
  }, [attempt]);

  let body;
  if (state.kind === 'loading' || state.kind === 'signed-out') {
    body = <Box sx={{ display: 'flex', justifyContent: 'center', py: 4 }}><CircularProgress aria-label={label('common.loading')} /></Box>;
  } else if (state.kind === 'forbidden') {
    body = <Stack spacing={1}><Typography>{label('common.no-access')}</Typography><Link href="/login/">{label('common.sign-in')}</Link></Stack>;
  } else if (state.kind !== 'ready') {
    body = <Alert severity="error" action={<Button color="inherit" size="small" onClick={() => setAttempt(attempt + 1)}>{label('common.try-again')}</Button>}>
      {label('common.could-not-load')}
    </Alert>;
  } else {
    const view = mineView(state.data);
    // Only a booking still to come can be asked about; none without a contact address.
    const ask = (line) => cancelMail(line, config.contact_email, config.member_name);
    body = <Box>
      <Typography component="h2" variant="h6" sx={{ mb: 1 }}>{label('me.bookings')}</Typography>
      {view.nothingComing && <Typography sx={{ color: 'text.secondary' }}>{label('me.none')}</Typography>}
      {view.coming.length > 0 && <Days days={view.comingDays} link={link} ask={ask} />}
      {view.earlier.length > 0 && <>
        <Typography component="h2" variant="h6" sx={{ mt: 3, mb: 1 }}>{label('me.earlier')}</Typography>
        <Box sx={{ opacity: 0.7 }}><Days days={view.earlierDays} link={link} ask={() => null} /></Box>
      </>}
    </Box>;
  }

  return <>
    <Typography component="h1" variant="h4">{label('place.me')}</Typography>
    {config.member_name && <Stack direction="row" spacing={1.5} sx={{ alignItems: 'center' }}>
      <Avatar sx={{ width: 48, height: 48, bgcolor: gray[50], color: NAVY, border: `2px solid ${NAVY}`, fontWeight: 600 }}>
        {initialsOf(config.member_name)}</Avatar>
      <Box sx={{ minWidth: 0 }}>
        <Typography sx={{ fontWeight: 600 }}>{config.member_name}</Typography>
        <Typography variant="body2" sx={{ color: 'text.secondary' }}>{config.organization}</Typography>
        {books && <Typography variant="body2" sx={{ color: 'text.secondary' }}>{books}</Typography>}
      </Box>
    </Stack>}
    {who.length > 0 && <Box component="dl" sx={{ m: 0, display: 'grid', gridTemplateColumns: 'max-content minmax(0, 1fr)', columnGap: 3, rowGap: 1 }}>
      {who.map((row) => <React.Fragment key={row.key}>
        <Typography component="dt" variant="body2" sx={{ color: 'text.secondary' }}>{row.label}</Typography>
        <Box component="dd" sx={{ m: 0, minWidth: 0, overflowWrap: 'anywhere' }}>
          {row.values.map((value, at) => <Typography key={`${at}|${value}`} variant="body2">{value}</Typography>)}
        </Box>
      </React.Fragment>)}
    </Box>}
    {body}
    <AccountRows admin={config.admin} />
  </>;
}
