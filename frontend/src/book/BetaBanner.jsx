// SPDX-License-Identifier: AGPL-3.0-only
import React from 'react';
import { Link, Typography } from '@mui/material';
import { styled } from '@mui/material/styles';
import { orange } from '../theme/themePrimitives';
import { label } from './labels.js';

// The beta bar at the very top of every page : shared by
// sign-in, the member pages and Admin, in the page's flow (it covers nothing and
// is not closable). Its words are the label `shell.beta` (randirandi/locale/en.json)
// around the organization's contact address (`config.contact_email`); the
// server-drawn pages read the same (presentation.py). No address, no bar.
// Colours: orange[700] under orange[50], 8.0:1.
const Bar = styled('div')(({ theme }) => ({
  boxSizing: 'border-box', width: '100%', flexShrink: 0, textAlign: 'center',
  padding: theme.spacing(0.5, 2), backgroundColor: '#331e0b', color: '#ffa057',
}));

export default function BetaBanner({ address }) {
  if (!address) return null;
  const [before, after = ''] = label('shell.beta', { address }).split(address);
  return <Bar>
    <Typography variant="caption" sx={{ color: 'inherit' }}>
      {before}<Link href={`mailto:${address}`} sx={{ color: 'inherit', textDecorationColor: 'inherit' }}>{address}</Link>{after}
    </Typography>
  </Bar>;
}
