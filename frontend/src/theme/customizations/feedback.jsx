// SPDX-License-Identifier: AGPL-3.0-only AND MIT
// Adapted from the official MUI Custom Theme, MIT. See accompanying UI input notices.
import { alpha } from '@mui/material/styles';
import { gray, green, orange, red } from '../themePrimitives';

/* eslint-disable import/prefer-default-export */
export const feedbackCustomizations = {
  MuiAlert: {
    styleOverrides: {
      root: ({ theme }) => ({
        borderRadius: 10,
        color: (theme.vars || theme).palette.text.primary,
        variants: [
          ['error', red],
          ['warning', orange],
          ['success', green],
        ].map(([severity, tone]) => ({
          props: { severity },
          style: {
            backgroundColor: alpha(tone[900], 0.5),
            border: `1px solid ${alpha(tone[300], 0.5)}`,
            '& .MuiAlert-icon': { color: tone[300] },
          },
        })).concat({
          // Neutral: blue means "yours" on the member pages .
          props: { severity: 'info' },
          style: {
            backgroundColor: alpha(gray[600], 0.5),
            border: `1px solid ${alpha(gray[400], 0.5)}`,
            '& .MuiAlert-icon': { color: gray[300] },
          },
        }),
      }),
    },
  },
  MuiDialog: {
    styleOverrides: {
      root: ({ theme }) => ({
        '& .MuiDialog-paper': {
          borderRadius: '10px',
          border: '1px solid',
          borderColor: (theme.vars || theme).palette.divider,
        },
      }),
    },
  },
  MuiLinearProgress: {
    styleOverrides: {
      root: ({ theme }) => ({
        height: 8,
        borderRadius: 8,
        backgroundColor: gray[200],
        ...theme.applyStyles('dark', {
          backgroundColor: gray[800],
        }),
      }),
    },
  },
};
