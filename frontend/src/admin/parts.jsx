// SPDX-License-Identifier: AGPL-3.0-only
import React from 'react';
import { Box, Chip, InputAdornment, LinearProgress, MenuItem, Stack, TableRow, TextField, Typography } from '@mui/material';
import SearchRounded from '@mui/icons-material/SearchRounded';
import { PAYMENT_COLORS, STATE_COLORS } from './bookings';
import { opensOnKey } from './members';

// Parts the Bookings, the Schedule and a booking's page share.

export const boxStyle = { border: 1, borderColor: 'divider', borderRadius: 2, overflowX: 'auto', bgcolor: 'background.paper' };
// A booking's number as a link, as the drawing has it: bold text. The theme's
// line under a link is off here; the keyboard focus outline stays.
export const plainLink = { fontWeight: 600, '&::before': { display: 'none' } };
export const cardStyle = { border: 1, borderColor: 'divider', borderRadius: 2, p: 1.5, bgcolor: 'background.paper', cursor: 'pointer' };

const focusRing = { '&:focus-visible': { outline: '2px solid', outlineColor: 'primary.main', outlineOffset: -2 } };
const keyOpens = (open) => (event) => {
  if (!opensOnKey(event)) return;
  event.preventDefault();
  open();
};

// A list's row, and its card on a phone, that open on a click and, with Tab to
// reach it, on Enter. The Members and a member's bookings use them.
export function OpenRow({ open, children }) {
  return <TableRow hover tabIndex={0} onClick={open} onKeyDown={keyOpens(open)} sx={{ cursor: 'pointer', ...focusRing }}>{children}</TableRow>;
}

// `flat`: a line inside a card, without a box of its own.
export function OpenCard({ open, flat = false, children }) {
  const style = flat ? { px: 2, py: 1.25, cursor: 'pointer' } : cardStyle;
  return <Box role="link" tabIndex={0} onClick={open} onKeyDown={keyOpens(open)} sx={{ ...style, ...focusRing }}>{children}</Box>;
}

export function StateChip({ state, label }) {
  return <Chip size="small" label={label} color={STATE_COLORS[state] ?? 'default'} variant="outlined" />;
}

export function PaymentChip({ state, label }) {
  return <Chip size="small" label={label} color={PAYMENT_COLORS[state] ?? 'default'} variant="outlined" />;
}

// The bar of a list that is read again: drawn in the gap between the filters and
// the list, so an idle one takes no room of its own.
export function Progress({ loading }) {
  return <Box sx={{ '&&': { mt: 0 }, height: 0, position: 'relative' }}>
    <LinearProgress sx={{ position: 'absolute', left: 0, right: 0, top: 6, visibility: loading ? 'visible' : 'hidden' }} />
  </Box>;
}

export function Card({ title, children, flush, action }) {
  const heading = <Typography variant="subtitle2" component="h2"
    sx={{ fontWeight: 600, flex: 1, mb: flush || action ? 0 : 1.25, p: flush ? 2 : 0, pb: flush ? 1 : 0 }}>{title}</Typography>;
  return <Box sx={{ border: 1, borderColor: 'divider', borderRadius: 2, p: flush ? 0 : 2, bgcolor: 'background.paper', overflow: 'hidden' }}>
    {action ? <Stack direction="row" sx={{ alignItems: 'center', mb: 1, mt: -0.5, mr: -0.5 }}>{heading}{action}</Stack> : heading}
    {flush ? <Box sx={{ overflowX: 'auto' }}>{children}</Box> : children}
  </Box>;
}

export function Field({ label, children }) {
  return <Stack direction="row" spacing={2} sx={{ py: 0.5 }}>
    <Typography variant="body2" sx={{ color: 'text.secondary', width: 120, flexShrink: 0 }}>{label}</Typography>
    <Typography variant="body2" component="div" sx={{ minWidth: 0, overflowWrap: 'anywhere' }}>{children}</Typography>
  </Stack>;
}

// The date filter: a choice, and the browser's own date picker for one day or
// a period. Dates are YYYY-MM-DD.
export function Dates({ label, choices, when, setWhen, from, setFrom, to, setTo, phone }) {
  const field = (text, value, set) => <TextField type="date" size="small" label={text} value={value}
    onChange={(e) => e.target.value && set(e.target.value)} slotProps={{ inputLabel: { shrink: true } }}
    sx={{ width: phone ? 'auto' : 160, flex: phone ? 1 : 'none', colorScheme: 'dark' }} />;
  const dated = when === 'day' || when === 'period';
  return <Stack direction="row" spacing={1.5} sx={{ alignItems: 'center' }}>
    <TextField select size="small" label={label} value={when} onChange={(e) => setWhen(e.target.value)}
      sx={{ width: phone && !dated ? '100%' : 150, flexShrink: 0 }}>
      {choices.map(([k, l]) => <MenuItem key={k} value={k}>{l}</MenuItem>)}
    </TextField>
    {when === 'day' && field('Date', from, setFrom)}
    {when === 'period' && <>{field('From', from, setFrom)}{field('To', to, setTo)}</>}
  </Stack>;
}

// The state filter: every state, or one of those the server offers.
export function StateFilter({ value, set, states, phone }) {
  return <TextField select size="small" label="State" value={value}
    onChange={(e) => set(e.target.value)} sx={{ minWidth: phone ? 140 : 160 }}>
    <MenuItem value="all">Every state</MenuItem>
    {states.map((s) => <MenuItem key={s.value} value={s.value}>{s.label}</MenuItem>)}
  </TextField>;
}

// A booking's link in a list: a plain click opens it in the page, a click with
// a key held opens the address.
export const openBooking = (open, number) => (event) => {
  event.stopPropagation();
  if (event.metaKey || event.ctrlKey || event.shiftKey || event.button === 1) return;
  event.preventDefault();
  open(number);
};

export function MemberSearch({ value, set, phone, placeholder = 'Member', widest = 260 }) {
  return <TextField size="small" placeholder={placeholder} value={value} onChange={(e) => set(e.target.value)}
    sx={{ flex: 1, maxWidth: phone ? 'none' : widest, minWidth: 160 }}
    slotProps={{ input: { startAdornment: <InputAdornment position="start"><SearchRounded fontSize="small" /></InputAdornment> } }} />;
}

// The filters: a row that wraps on a wide screen; on a phone the date filter
// on its own and the rest below.
export function Filters({ phone, dates, children }) {
  if (phone) {
    return <Stack spacing={1.5}>{dates}<Stack direction="row" spacing={1.5}>{children}</Stack></Stack>;
  }
  return <Stack direction="row" sx={{ flexWrap: 'wrap', gap: 1.5, alignItems: 'center' }}>{dates}{children}</Stack>;
}
