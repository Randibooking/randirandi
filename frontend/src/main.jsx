// SPDX-License-Identifier: AGPL-3.0-only
import React from 'react';
import { createRoot } from 'react-dom/client';
import createCache from '@emotion/cache';
import { CacheProvider } from '@emotion/react';
import {
  Alert, Autocomplete, Box, Button, Card, CssBaseline, FormControl, FormLabel,
  Link, Stack, TextField, Typography,
} from '@mui/material';
import { styled } from '@mui/material/styles';
import AppTheme from './theme/AppTheme';
import AdminShell from './admin/AdminShell';
import MemberShell from './book/MemberShell';
import SignInAbout from './book/SignInAbout';
import BetaBanner from './book/BetaBanner';
import PageFooter from './book/PageFooter';
import PublicPage from './book/PublicPage';
import { label, setLabels } from './book/labels.js';
import { red } from './theme/themePrimitives';
import { COUNTRIES, countryLabel, joinPhone } from './login/phone';
import { SCREEN_AFTER, numberWords, openOn, switchLabel } from './login/screen';
import './fonts.css';
import './fallback.css';

// Layout adapted from the selected official MUI SignIn.js (MIT).
// Password/social/demo controls and appearance switching are removed.
const SignInCard = styled(Card)(({ theme }) => ({
  display: 'flex', flexDirection: 'column', alignSelf: 'center', width: '100%',
  padding: theme.spacing(3), gap: theme.spacing(2), boxSizing: 'border-box',
  [theme.breakpoints.up('sm')]: { maxWidth: 450, padding: theme.spacing(4) },
  [theme.breakpoints.up('md')]: { flex: '0 1 450px', width: 'auto' },
}));
// An example, not a value: clearly fainter than typed text (3.6:1 on the
// field against 15:1 for typed text; gray[400] was 6:1), so it never reads as
// an address someone already added.
const hint = { '& input::placeholder': { color: 'hsl(220, 14%, 50%)', opacity: 1 } };
// A link in the sign-in card: sand 12 like the text, a plain 2px underline always
// visible (the address inside a sentence, and the switch under Continue).
const addressLink = {
  display: 'inline', textDecoration: 'underline', textDecorationThickness: '2px',
  textUnderlineOffset: '4px', '&::before': { display: 'none' },
  '&:hover': { textDecorationThickness: '3px' },
};
// Every sign-in screen: Randi's words beside the card (below it on a phone,
// the MUI sign-in-side template), and the slim bar at the page's foot.
const Group = styled(Stack)(({ theme }) => ({
  flex: 1, alignItems: 'center', justifyContent: 'center', boxSizing: 'border-box',
  flexDirection: 'column-reverse', gap: theme.spacing(6), padding: theme.spacing(2),
  [theme.breakpoints.up('sm')]: { padding: theme.spacing(4), gap: theme.spacing(12) },
  [theme.breakpoints.up('md')]: { flexDirection: 'row' },
}));
function Shell({ config, children }) {
  return <Stack sx={{ minHeight: '100dvh' }}>
    <BetaBanner address={config.contact_email} />
    <Group>
      <SignInAbout organization={config.organization} />
      {children}
    </Group>
    <PageFooter config={config} links />
  </Stack>;
}

function Brand({ logo }) {
  return <Stack direction="row" spacing={1.5} sx={{ alignItems: 'center' }}>
    <Box component="img" src={logo} alt="" sx={{ width: 64, height: 64, flexShrink: 0 }} />
    <Typography sx={{ fontWeight: 600 }}>RandiRandi</Typography>
  </Stack>;
}

function Login({ config, initialValue }) {
  const [pending, setPending] = React.useState(false);
  const invalid = config.errors.length > 0;
  // A remembered value that starts with "+" opens on the mobile number.
  const opened = React.useMemo(() => openOn(initialValue), [initialValue]);
  const [screen, setScreen] = React.useState(opened.screen);
  const [email, setEmail] = React.useState(opened.email);
  const [country, setCountry] = React.useState(opened.country);
  const [number, setNumber] = React.useState(opened.number);
  const phone = screen === 'phone';
  const words = numberWords(config.contact_email);
  const described = invalid ? 'identifier-help identifier-error' : 'identifier-help';
  // Restoring a page from the browser's back/forward cache must restore its form.
  React.useEffect(() => {
    const ready = () => setPending(false);
    window.addEventListener('pageshow', ready);
    return () => window.removeEventListener('pageshow', ready);
  }, []);
  return <>
    <Typography variant="body2" sx={{ color: 'text.secondary' }}>{config.organization}</Typography>
    <Typography component="h1" variant="h4">{label('login.title')}</Typography>
    {config.unavailable && <Alert severity="error">
      {label('login.unavailable')}
    </Alert>}
    <Box component="form" method="post" action={config.action}
      onSubmit={() => setPending(true)}
      sx={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
      <input type="hidden" name="csrfmiddlewaretoken" value={config.csrf} />
      <FormControl>
        {phone && <input type="hidden" name="identifier" value={joinPhone(country.code, number)} />}
        <FormLabel htmlFor="id_identifier" sx={{ mb: 1 }}>{phone ? label('login.mobile') : label('login.email')}</FormLabel>
        {phone ? <Stack direction="row" spacing={1} sx={{ alignItems: 'flex-start' }}>
          <Autocomplete disableClearable autoHighlight sx={{
            width: 120, flexShrink: 0,
            // The same height as the number beside it: the field's padding, not the picker's.
            '&& .MuiOutlinedInput-root': { padding: '8px 39px 8px 12px' },
            '&& .MuiOutlinedInput-root .MuiAutocomplete-input': { padding: 0 },
          }}
            options={COUNTRIES} value={country} onChange={(_, value) => setCountry(value)}
            getOptionLabel={(option) => option.code} isOptionEqualToValue={(a, b) => a.region === b.region}
            filterOptions={(options, { inputValue }) => {
              const typed = inputValue.trim().toLowerCase();
              return typed === country.code ? options : options.filter((o) => countryLabel(o).toLowerCase().includes(typed));
            }}
            renderOption={({ key, ...props }, option) => <li key={key} {...props}>{countryLabel(option)}</li>}
            slotProps={{ popupIndicator: { sx: { width: 'auto', height: 'auto', border: 'none', background: 'none', '&:hover': { background: 'none' } } } }}
            renderInput={(params) => <TextField {...params} slotProps={{
              ...params.slotProps,
              htmlInput: { ...params.slotProps.htmlInput, 'aria-label': label('login.country') },
            }} />} />
          <TextField id="id_identifier" required fullWidth type="tel" value={number}
            onChange={(event) => setNumber(event.target.value)} error={invalid} autoComplete="tel-national" sx={hint}
            slotProps={{ htmlInput: {
              maxLength: 40, spellCheck: false, 'aria-describedby': described,
              placeholder: '987 65 432',
            } }} />
        </Stack> : <TextField id="id_identifier" name="identifier" required fullWidth
          value={email} onChange={(event) => setEmail(event.target.value)}
          error={invalid} autoComplete="username" sx={hint}
          slotProps={{ htmlInput: {
            placeholder: 'name@domain',
            maxLength: 254, inputMode: 'email', autoCapitalize: 'none', spellCheck: false,
            'aria-describedby': invalid ? 'identifier-error' : undefined,
          } }} />}
        {phone && <Typography id="identifier-help" variant="caption" sx={{ mt: 1, color: 'text.secondary' }}>
          {words.before}<Link href={words.href} sx={addressLink}>{words.address}</Link>{words.after}
        </Typography>}
        {invalid && <Typography id="identifier-error" role="alert" variant="body2" sx={{ color: red[300] }}>
          {config.errors.join(' ')}
        </Typography>}
      </FormControl>
      <Button type="submit" variant="contained" disabled={pending}>
        {pending ? label('login.wait') : label('login.continue')}
      </Button>
      {/* Under Continue, read after the field and not before it. */}
      <Link component="button" type="button" variant="body2" sx={{ ...addressLink, alignSelf: 'center', mt: 1 }}
        onClick={() => setScreen(SCREEN_AFTER[screen])}>
        {switchLabel(screen)}
      </Link>
    </Box>
  </>;
}

function Code({ config, initialValue }) {
  const authenticator = config.screen === 'authenticator';
  const [pending, setPending] = React.useState(false);
  React.useEffect(() => {
    const ready = () => setPending(false);
    window.addEventListener('pageshow', ready);
    return () => window.removeEventListener('pageshow', ready);
  }, []);
  return <>
    <Typography variant="body2" sx={{ color: 'text.secondary' }}>{config.organization}</Typography>
    <Typography component="h1" variant="h4">{authenticator ? label(config.enroll ? 'login.auth-setup-title' : 'login.auth-title') : label('login.code-title')}</Typography>
    <Typography id="code-help" variant="body2" sx={{ color: 'text.secondary' }}>
      {authenticator
        ? label(config.enroll ? 'login.auth-setup-help' : 'login.auth-help')
        : label('login.code-help')}
    </Typography>
    {authenticator && config.enroll && <>
      <Box component="img" src={config.qr} alt={label('login.auth-qr')}
        sx={{ width: 220, height: 220, maxWidth: '100%', alignSelf: 'center' }} />
      <Typography variant="body2">{label('login.auth-key')}</Typography>
      <Typography component="code" sx={{ overflowWrap: 'anywhere' }}>{config.setupKey}</Typography>
    </>}
    <Box component="form" method="post" action={config.action}
      onSubmit={() => setPending(true)} sx={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
      <input type="hidden" name="csrfmiddlewaretoken" value={config.csrf} />
      <FormControl>
        <FormLabel htmlFor="id_code">{label(authenticator ? 'login.auth-code' : 'login.code-label')}</FormLabel>
        <TextField id="id_code" name="code" required fullWidth defaultValue={initialValue}
          autoComplete="one-time-code" error={Boolean(config.error)}
          slotProps={{ htmlInput: { inputMode: 'numeric', pattern: '[0-9]{6}', maxLength: 6,
            'aria-describedby': config.error ? 'code-help code-error' : 'code-help' } }} />
      </FormControl>
      {config.error && <Alert id="code-error" severity="error">{config.error}</Alert>}
      <Button type="submit" variant="contained" disabled={pending}>
        {pending ? label('login.wait') : label('login.title')}
      </Button>
    </Box>
    <Link href="/login/">{label(authenticator ? 'login.auth-restart' : 'login.code-new')}</Link>
  </>;
}

function Home({ config }) {
  return <>
    <Typography variant="body2" sx={{ color: 'text.secondary' }}>{config.organization}</Typography>
    <Typography component="h1" variant="h4">{label('login.signed-in')}</Typography>
    <Box component="form" method="post" action="/logout/">
      <input type="hidden" name="csrfmiddlewaretoken" value={config.csrf} />
      <Button type="submit" variant="contained">{label('me.sign-out')}</Button>
    </Box>
  </>;
}

function Status({ config }) {
  return <>
    <Typography component="h1" variant="h4">{config.title}</Typography>
    <Typography sx={{ color: 'text.secondary' }}>{config.message}</Typography>
    <Button href={config.action} variant="contained">{label('login.back')}</Button>
  </>;
}

// Enhance a real server form. If loading fails, its labels, CSRF token and
// native POST still work. Preserve anything typed before the bundle arrived.
const root = document.getElementById('rr-root');
const data = document.getElementById('rr-config');
if (root && data) {
  const config = JSON.parse(data.textContent);
  // The organization's own texts for the member pages; none on login.
  setLabels(config.labels);
  const initialValue = root.querySelector(['code', 'authenticator'].includes(config.screen) ? '[name="code"]' : '[name="identifier"]')?.value || config.identifier || '';
  const nonce = document.querySelector('meta[name="csp-nonce"]').content;
  const cache = createCache({ key: 'rr', nonce });
  createRoot(root).render(
    <CacheProvider value={cache}><AppTheme><CssBaseline />
      {config.screen === 'admin' ? <AdminShell config={config} />
        : ['book', 'me', 'cart', 'order', 'prices', 'events'].includes(config.screen) || (config.screen === 'conditions' && config.member) ? <MemberShell config={config} />
        : ['prices-public', 'conditions'].includes(config.screen) ? <PublicPage config={config} /> : <Shell config={config}><SignInCard variant="outlined">
        <Brand logo={config.logo} />
        {config.screen === 'login'
          ? <Login config={config} initialValue={initialValue} />
          : ['code', 'authenticator'].includes(config.screen) ? <Code config={config} initialValue={initialValue} />
          : config.screen === 'home' ? <Home config={config} />
          : <Status config={config} />}
      </SignInCard></Shell>}
    </AppTheme></CacheProvider>,
  );
}
