// SPDX-License-Identifier: AGPL-3.0-only
import React from 'react';
import { Box, Button, Stack, Typography } from '@mui/material';
import BetaBanner from './BetaBanner';
import PageFooter from './PageFooter';
import PricesPage from './PricesPage';
import { label } from './labels.js';

// The price list and the booking conditions, public pages of the organization
// of the address : readable without signing in, from the footer of every
// page. The conditions' words come in the page as the server filled them in
// (an order's own frozen text is the same page); the price list is the member
// price list from the public answers. A visitor who is not signed in has Sign in
// in the header; a member of the organization gets these pages in the member
// shell instead (MemberShell.jsx), so this page is the visitor's only.
const PAGE_WIDTH = 720;

export function ConditionsText({ text }) {
  return <Stack spacing={2}>
    <Typography component="h1" variant="h4">{text.title}</Typography>
    <Typography>{text.intro}</Typography>
    {text.sections.map((section) => <Box key={section.heading}>
      <Typography component="h2" variant="h6">{section.heading}</Typography>
      <Typography>{section.text}</Typography>
    </Box>)}
  </Stack>;
}

export default function PublicPage({ config }) {
  const prices = config.screen === 'prices-public';
  return <Box sx={{ minHeight: '100dvh', display: 'flex', flexDirection: 'column' }}>
    <BetaBanner address={config.contact_email} />
    <Box sx={{ flex: 1, display: 'flex', flexDirection: 'column', width: '100%', maxWidth: 1240, mx: 'auto' }}>
      <Stack component="header" direction="row" spacing={1.5}
        sx={{ alignItems: 'center', px: 2, py: 1, borderBottom: 1, borderColor: 'divider' }}>
        <Box component="img" src={config.logo} alt="" sx={{ width: { xs: 48, md: 64 }, height: { xs: 48, md: 64 }, flexShrink: 0 }} />
        <Box sx={{ minWidth: 0, flexGrow: 1 }}>
          <Typography noWrap sx={{ fontWeight: 600 }}>{label('shell.product')}</Typography>
          <Typography variant="body2" sx={{ color: 'text.secondary' }}>{config.organization}</Typography>
        </Box>
        <Button href="/login/" sx={{ color: 'text.primary', whiteSpace: 'nowrap', flexShrink: 0 }}>{label('common.sign-in')}</Button>
      </Stack>
      <Box component="main" sx={{ flexGrow: 1, p: 2 }}>
        <Stack spacing={2} sx={{ maxWidth: PAGE_WIDTH, mx: 'auto' }}>
          {prices ? <PricesPage config={config} publicList />
            : <ConditionsText text={config.conditions} />}
        </Stack>
      </Box>
      <PageFooter config={config} links />
    </Box>
  </Box>;
}
