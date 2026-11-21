// SPDX-License-Identifier: AGPL-3.0-only
import React from 'react';
import { Link, Stack, Typography } from '@mui/material';
import { styled } from '@mui/material/styles';
import { label } from './labels.js';

// The footer of every page : the slim bar of the sign-in page
// (a divider line above, company left, the links right; stacked and centred on a
// narrow phone), shared by sign-in, the member pages and Admin. `links` adds
// Prices and Booking conditions; Admin's own pages leave them out. `lift` is the
// space kept under it, for what floats at the bottom of a phone (the bottom
// navigation and the cart pill). Safe-area padding at the bottom.
const Bar = styled('footer', { shouldForwardProp: (name) => name !== 'lift' })(({ theme, lift }) => ({
  display: 'flex', flexDirection: 'column', alignItems: 'center', gap: theme.spacing(1),
  boxSizing: 'border-box', width: '100%', flexShrink: 0, borderTop: `1px solid ${theme.palette.divider}`,
  padding: theme.spacing(2, 2, 'calc(16px + env(safe-area-inset-bottom))'),
  marginBottom: lift || 0,
  [theme.breakpoints.up('sm')]: {
    flexDirection: 'row', justifyContent: 'space-between', paddingInline: theme.spacing(4),
  },
}));

export default function PageFooter({ config, lift, links }) {
  const shown = Boolean(links && config.footer_links);
  return <Bar lift={lift}>
    <Typography variant="body2" sx={{ color: 'text.secondary' }}>{label('shell.company')}</Typography>
    <Stack direction="row" sx={{ flexWrap: 'wrap', justifyContent: 'center', columnGap: 3, rowGap: 1 }}>
      {shown && <Link href="/prices/" variant="body2" sx={{ color: 'text.secondary' }}>{label('shell.prices')}</Link>}
      {shown && <Link href="/conditions/" variant="body2" sx={{ color: 'text.secondary' }}>{label('shell.conditions')}</Link>}
      {config.source && <Link href={config.source} target="_blank" rel="noopener noreferrer" variant="body2" sx={{ color: 'text.secondary' }}>{label('shell.source')} {config.version}</Link>}
      <Link href={config.documentation} target="_blank" rel="noopener noreferrer" variant="body2"
        sx={{ color: 'text.secondary' }}>{label('shell.documentation')}</Link>
    </Stack>
  </Bar>;
}
