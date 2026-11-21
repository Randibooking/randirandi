// SPDX-License-Identifier: AGPL-3.0-only
import React from 'react';
import {
  Box, Dialog, Drawer, IconButton, Link, Stack, Typography,
} from '@mui/material';
import CloseRounded from '@mui/icons-material/CloseRounded';
import MailOutlineRounded from '@mui/icons-material/MailOutlineRounded';
import { gray } from '../theme/themePrimitives';
import { label } from './labels.js';
import { contactHref, dateWords } from './words.js';

// Another member's booking, opened from the calendar :
// the member's name (just the name), where and when, and the organization's
// contact address to ask for contact by email. Never the member's own email or
// phone: the server sends only the name. A dialog on a wide screen, a bottom
// sheet on a phone.
export const showBooked = (booked) => window.dispatchEvent(new CustomEvent('rr-booked', { detail: booked }));

export default function BookedSheet({ wide, contact }) {
  const [booked, setBooked] = React.useState(null);
  const [open, setOpen] = React.useState(false);
  React.useEffect(() => {
    const show = (event) => { setBooked(event.detail); setOpen(true); };
    window.addEventListener('rr-booked', show);
    return () => window.removeEventListener('rr-booked', show);
  }, []);
  const title = React.useId();
  const close = () => setOpen(false);
  const content = booked && <>
    <Stack direction="row" sx={{ alignItems: 'flex-start', gap: 1 }}>
      <Box sx={{ flex: 1, minWidth: 0 }}>
        <Typography id={title} variant="h6" sx={{ fontWeight: 600 }}>{booked.name}</Typography>
        <Typography sx={{ color: 'text.secondary' }}>{booked.place}</Typography>
        <Typography sx={{ color: 'text.secondary' }}>{`${dateWords(booked.date)}, ${booked.hours}`}</Typography>
      </Box>
      <IconButton size="small" onClick={close} aria-label={label('common.close')}><CloseRounded fontSize="small" /></IconButton>
    </Stack>
    <Box sx={{ mt: 2, pt: 1.5, borderTop: 1, borderColor: 'divider' }}>
      <Typography variant="caption" sx={{ color: 'text.secondary' }}>{label('booked.contact')}</Typography>
      <Stack direction="row" spacing={1} sx={{ alignItems: 'center' }}>
        <MailOutlineRounded sx={{ fontSize: 18, color: 'text.secondary' }} />
        <Link href={contactHref(booked, contact)} sx={{ color: 'text.primary' }}>{contact}</Link>
      </Stack>
    </Box>
  </>;
  if (wide) {
    return <Dialog open={open} onClose={close} aria-labelledby={title} fullWidth maxWidth="xs"
      slotProps={{ paper: { sx: { bgcolor: 'background.paper', backgroundImage: 'none' } } }}>
      <Box sx={{ p: 3 }}>{content}</Box>
    </Dialog>;
  }
  return <Drawer anchor="bottom" open={open} onClose={close}
    slotProps={{ paper: { role: 'dialog', 'aria-modal': true, 'aria-labelledby': title, sx: {
      bgcolor: 'background.paper', backgroundImage: 'none', borderTop: 1, borderColor: 'divider',
      borderRadius: '18px 18px 0 0', p: 2.5, pb: 'calc(24px + env(safe-area-inset-bottom))',
    } } }}>
    <Box sx={{ width: 40, height: 4, borderRadius: 2, bgcolor: gray[600], mx: 'auto', mb: 1.5 }} />
    {content}
  </Drawer>;
}
