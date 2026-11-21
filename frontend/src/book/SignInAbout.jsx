// SPDX-License-Identifier: AGPL-3.0-only
import React from 'react';
import { Stack, Typography } from '@mui/material';
import EventAvailableRounded from '@mui/icons-material/EventAvailableRounded';
import TouchAppRounded from '@mui/icons-material/TouchAppRounded';
import PaymentsRounded from '@mui/icons-material/PaymentsRounded';
import { label } from './labels.js';

// Beside the sign-in card (below it on a phone): the MUI sign-in-side
// template's Content. Who Randi is, then three items, title only.
const ITEMS = [
  { key: 'shell.free', Icon: EventAvailableRounded },
  { key: 'shell.book', Icon: TouchAppRounded },
  { key: 'shell.pay', Icon: PaymentsRounded },
];

// `organization`: the co-op's name Randi's intro names; the error page has
// none and names the product.
export default function SignInAbout({ organization }) {
  return <Stack component="section" sx={{ flex: { xs: '0 0 auto', md: '1 1 0' }, width: '100%', minWidth: 0, alignSelf: 'center', gap: 4, maxWidth: 450 }}>
    <Stack sx={{ gap: 1 }}>
      <Typography component="p" variant="h5" sx={{ fontWeight: 'medium' }}>{label('shell.name')}</Typography>
      <Typography variant="body2" sx={{ color: 'text.secondary' }}>{label('shell.intro', { organization: organization || label('shell.product') })}</Typography>
    </Stack>
    {ITEMS.map(({ key, Icon }) => <Stack key={key} direction="row" sx={{ gap: 2, alignItems: 'center' }}>
      <Icon aria-hidden="true" sx={{ color: 'text.secondary' }} />
      <Typography sx={{ fontWeight: 'medium' }}>{label(key)}</Typography>
    </Stack>)}
  </Stack>;
}
