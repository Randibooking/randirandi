// SPDX-License-Identifier: AGPL-3.0-only AND MIT
// Adapted from official MUI Custom Theme (MIT): fixed dark palette; demo switches removed.
import React from 'react';
import { ThemeProvider, createTheme } from '@mui/material/styles';
import { inputsCustomizations } from './customizations/inputs';
import { dataDisplayCustomizations } from './customizations/dataDisplay';
import { feedbackCustomizations } from './customizations/feedback';
import { navigationCustomizations } from './customizations/navigation';
import { surfacesCustomizations } from './customizations/surfaces';
import { colorSchemes, typography, shadows, shape } from './themePrimitives';
export const theme = createTheme({
  palette: { ...colorSchemes.dark.palette, mode: 'dark' },
  typography, shadows, shape,
  components: { ...inputsCustomizations, ...dataDisplayCustomizations,
    ...feedbackCustomizations, ...navigationCustomizations, ...surfacesCustomizations },
});
export default function AppTheme({ children }) {
  return <ThemeProvider theme={theme}>{children}</ThemeProvider>;
}
