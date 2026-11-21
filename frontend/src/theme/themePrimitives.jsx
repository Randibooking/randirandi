// SPDX-License-Identifier: AGPL-3.0-only AND MIT
// Adapted from the official MUI Custom Theme, MIT. See accompanying UI input notices.
import { createTheme, alpha } from '@mui/material/styles';

const defaultTheme = createTheme();

const customShadows = [...defaultTheme.shadows];

export const brand = {
  50: '#eeeeec',
  100: '#eeeeec',
  200: '#b5b3ad',
  300: '#eeeeec',
  400: '#b5b3ad',
  500: '#b5b3ad',
  600: '#eeeeec',
  700: '#494844',
  800: '#222221',
  900: '#191918',
};

export const gray = {
  50: '#eeeeec',
  100: '#eeeeec',
  200: '#b5b3ad',
  300: '#b5b3ad',
  400: '#b5b3ad',
  500: '#b5b3ad',
  600: '#494844',
  700: '#31312e',
  800: '#191918',
  900: '#111110',
};

export const green = {
  50: '#c2f0c2',
  100: '#c2f0c2',
  200: '#71d083',
  300: '#71d083',
  400: '#46a758',
  500: '#53b365',
  600: '#53b365',
  700: '#3e7949',
  800: '#366740',
  900: '#1b2a1e',
};

export const orange = {
  50: '#ffe0c2',
  100: '#ffe0c2',
  200: '#ffa057',
  300: '#ffa057',
  400: '#f76b15',
  500: '#ff801f',
  600: '#ff801f',
  700: '#a35829',
  800: '#7e451d',
  900: '#331e0b',
};

export const red = {
  50: '#ffd1d9',
  100: '#ffd1d9',
  200: '#ff9592',
  300: '#ff9592',
  400: '#e5484d',
  500: '#ec5d5e',
  600: '#ec5d5e',
  700: '#b54548',
  800: '#8c333a',
  900: '#3b1219',
};

// Scheme C's closed roles (scheme-c-2026-10-24/scheme.json): the red scale, steps 3
// (fill), 4 (the hatch beside it), 9 (edge, 3:1 on the card and the page) and 11 (text). Every place that draws
// Closed takes its colours from here.
export const closed = {
  fill: red[900], hatch: '#500f1c', edge: red[400], text: red[200],
};
export const closedStripes = (stripe, period) => `repeating-linear-gradient(135deg, ${closed.fill} 0 ${stripe}px, ${closed.hatch} ${stripe}px ${period}px)`;

// Someone else's booked time, hatched grey wherever a member sees it (T3,
// 2026-11-15: flat grey read close to free). The stripes are as light as the
// text allows: 1.9:1 to the fill, and the text, #eeeeec, 5.1:1 on the stripes
// (design review, 2026-11-16: 1.5:1 read as one grey).
export const booked = { fill: '#3b3a37', hatch: '#66645e' };
export const bookedStripes = (stripe, period) => `repeating-linear-gradient(135deg, ${booked.hatch} 0 ${stripe}px, ${booked.fill} ${stripe}px ${period}px)`;

export const getDesignTokens = (mode) => {
  customShadows[1] =
    mode === 'dark'
      ? 'hsla(220, 30%, 5%, 0.7) 0px 4px 16px 0px, hsla(220, 25%, 10%, 0.8) 0px 8px 16px -5px'
      : 'hsla(220, 30%, 5%, 0.07) 0px 4px 16px 0px, hsla(220, 25%, 10%, 0.07) 0px 8px 16px -5px';

  return {
    palette: {
      mode,
      primary: {
        light: brand[200],
        main: brand[400],
        dark: brand[700],
        contrastText: brand[50],
        ...(mode === 'dark' && {
          contrastText: brand[50],
          light: brand[300],
          main: brand[400],
          dark: brand[700],
        }),
      },
      info: {
        light: brand[100],
        main: brand[300],
        dark: brand[600],
        contrastText: gray[50],
        ...(mode === 'dark' && {
          contrastText: brand[300],
          light: brand[500],
          main: brand[700],
          dark: brand[900],
        }),
      },
      warning: {
        light: orange[300],
        main: orange[400],
        dark: orange[800],
        ...(mode === 'dark' && {
          light: orange[400],
          main: orange[500],
          dark: orange[700],
        }),
      },
      error: {
        light: red[300],
        main: red[400],
        dark: red[800],
        // Readable on the dark field: a field's label and border take main.
        ...(mode === 'dark' && {
          light: red[200],
          main: red[300],
          dark: red[400],
        }),
      },
      success: {
        light: green[300],
        main: green[400],
        dark: green[800],
        ...(mode === 'dark' && {
          light: green[400],
          main: green[500],
          dark: green[700],
        }),
      },
      grey: {
        ...gray,
      },
      divider: mode === 'dark' ? gray[600] : alpha(gray[300], 0.4),
      background: {
        default: 'hsl(0, 0%, 99%)',
        paper: 'hsl(220, 35%, 97%)',
        // a dark grey background, a new step between gray.800 (6 %) and
        // gray.700 (25 %); cards one step lighter.
        ...(mode === 'dark' && { default: '#222221', paper: gray[700] }),
      },
      text: {
        primary: gray[800],
        secondary: gray[600],
        warning: orange[400],
        ...(mode === 'dark' && {
          primary: '#eeeeec',
          secondary: gray[300],
        }),
      },
      action: {
        hover: alpha(gray[200], 0.2),
        selected: `${alpha(gray[200], 0.3)}`,
        ...(mode === 'dark' && {
          hover: alpha(gray[600], 0.2),
          selected: alpha(gray[600], 0.3),
        }),
      },
    },
    typography: {
      fontFamily: 'Inter, sans-serif',
      h1: {
        fontSize: defaultTheme.typography.pxToRem(48),
        fontWeight: 600,
        lineHeight: 1.2,
        letterSpacing: -0.5,
      },
      h2: {
        fontSize: defaultTheme.typography.pxToRem(36),
        fontWeight: 600,
        lineHeight: 1.2,
      },
      h3: {
        fontSize: defaultTheme.typography.pxToRem(30),
        lineHeight: 1.2,
      },
      h4: {
        fontSize: defaultTheme.typography.pxToRem(24),
        fontWeight: 600,
        lineHeight: 1.5,
      },
      h5: {
        fontSize: defaultTheme.typography.pxToRem(20),
        fontWeight: 600,
      },
      h6: {
        fontSize: defaultTheme.typography.pxToRem(18),
        fontWeight: 600,
      },
      subtitle1: {
        fontSize: defaultTheme.typography.pxToRem(18),
      },
      subtitle2: {
        fontSize: defaultTheme.typography.pxToRem(14),
        fontWeight: 500,
      },
      body1: {
        fontSize: defaultTheme.typography.pxToRem(14),
      },
      body2: {
        fontSize: defaultTheme.typography.pxToRem(14),
        fontWeight: 400,
      },
      caption: {
        fontSize: defaultTheme.typography.pxToRem(12),
        fontWeight: 400,
      },
    },
    shape: {
      borderRadius: 8,
    },
    shadows: customShadows,
  };
};

export const colorSchemes = {
  light: {
    palette: {
      primary: {
        light: brand[200],
        main: brand[400],
        dark: brand[700],
        contrastText: brand[50],
      },
      info: {
        light: brand[100],
        main: brand[300],
        dark: brand[600],
        contrastText: gray[50],
      },
      warning: {
        light: orange[300],
        main: orange[400],
        dark: orange[800],
      },
      error: {
        light: red[300],
        main: red[400],
        dark: red[800],
      },
      success: {
        light: green[300],
        main: green[400],
        dark: green[800],
      },
      grey: {
        ...gray,
      },
      divider: alpha(gray[300], 0.4),
      background: {
        default: 'hsl(0, 0%, 99%)',
        paper: 'hsl(220, 35%, 97%)',
      },
      text: {
        primary: gray[800],
        secondary: gray[600],
        warning: orange[400],
      },
      action: {
        hover: alpha(gray[200], 0.2),
        selected: `${alpha(gray[200], 0.3)}`,
      },
      baseShadow:
        'hsla(220, 30%, 5%, 0.07) 0px 4px 16px 0px, hsla(220, 25%, 10%, 0.07) 0px 8px 16px -5px',
    },
  },
  dark: {
    palette: {
      primary: {
        contrastText: brand[50],
        light: brand[300],
        main: brand[400],
        dark: brand[700],
      },
      info: {
        contrastText: brand[300],
        light: brand[500],
        main: brand[700],
        dark: brand[900],
      },
      warning: {
        light: orange[400],
        main: orange[500],
        dark: orange[700],
      },
      error: {
        light: red[200],
        main: red[300],
        dark: red[400],
      },
      success: {
        light: green[400],
        main: green[500],
        dark: green[700],
      },
      grey: {
        ...gray,
      },
      divider: gray[600],
      background: {
        default: '#222221',
        paper: gray[700],
      },
      text: {
        primary: '#eeeeec',
        secondary: gray[300],
      },
      action: {
        hover: alpha(gray[600], 0.2),
        selected: alpha(gray[600], 0.3),
      },
      baseShadow:
        'hsla(220, 30%, 5%, 0.7) 0px 4px 16px 0px, hsla(220, 25%, 10%, 0.8) 0px 8px 16px -5px',
    },
  },
};

export const typography = {
  fontFamily: 'Inter, sans-serif',
  h1: {
    fontSize: defaultTheme.typography.pxToRem(48),
    fontWeight: 600,
    lineHeight: 1.2,
    letterSpacing: -0.5,
  },
  h2: {
    fontSize: defaultTheme.typography.pxToRem(36),
    fontWeight: 600,
    lineHeight: 1.2,
  },
  h3: {
    fontSize: defaultTheme.typography.pxToRem(30),
    lineHeight: 1.2,
  },
  h4: {
    fontSize: defaultTheme.typography.pxToRem(24),
    fontWeight: 600,
    lineHeight: 1.5,
  },
  h5: {
    fontSize: defaultTheme.typography.pxToRem(20),
    fontWeight: 600,
  },
  h6: {
    fontSize: defaultTheme.typography.pxToRem(18),
    fontWeight: 600,
  },
  subtitle1: {
    fontSize: defaultTheme.typography.pxToRem(18),
  },
  subtitle2: {
    fontSize: defaultTheme.typography.pxToRem(14),
    fontWeight: 500,
  },
  body1: {
    fontSize: defaultTheme.typography.pxToRem(14),
  },
  body2: {
    fontSize: defaultTheme.typography.pxToRem(14),
    fontWeight: 400,
  },
  caption: {
    fontSize: defaultTheme.typography.pxToRem(12),
    fontWeight: 400,
  },
};

export const shape = {
  borderRadius: 8,
};

const defaultShadows = [
  'none',
  'var(--template-palette-baseShadow)',
  ...defaultTheme.shadows.slice(2),
];

export const shadows = defaultShadows;
