// SPDX-License-Identifier: AGPL-3.0-only
import React from 'react';
import {
  Alert, Box, CircularProgress, Link, Stack, Table, TableBody, TableCell, TableContainer, TableHead, TableRow, Typography,
} from '@mui/material';
import ApartmentRounded from '@mui/icons-material/ApartmentRounded';
import BedtimeOutlined from '@mui/icons-material/BedtimeOutlined';
import GroupsRounded from '@mui/icons-material/GroupsRounded';
import HomeRounded from '@mui/icons-material/HomeRounded';
import PersonRounded from '@mui/icons-material/PersonRounded';
import WbSunnyOutlined from '@mui/icons-material/WbSunnyOutlined';
import WorkRounded from '@mui/icons-material/WorkRounded';
import { readCalendar, readChoices, readPublicCalendar, readPublicChoices } from './api';
import { browserToday } from './BookMonth';
import { typeOrder } from './cart.js';
import { categoryIcon } from './categories.js';
import { calendarQuery, filterOf, readAddress } from './filter.js';
import { label } from './labels.js';
import { priceBook, priceColumns, priceQuery } from './prices.js';

// The prices page ("a linkl to a pricing page (that we also have to
// desing)"), a matrix ("design the price list as matrix. Privat and Work
// two cols."): a column per category members book as, the standard role's
// ("Remove "Internal" from the Price List"), a row per container and its
// worked examples, from the calendar's own price tiers. Reached from the
// booking dialog's "How prices work"; a view of Book, so the dialog is still
// open on the way back.
const ICONS = {
  person: PersonRounded, work: WorkRounded, building: ApartmentRounded, home: HomeRounded, group: GroupsRounded,
};
// Two weeks hold every weekday of every container.
const DAYS = 14;
const CELL = { px: { xs: 1, sm: 2 }, py: 1.25, verticalAlign: 'top' };
// Amounts, lengths and hours never break inside ("1 000 kr", "4 h", "10:00–22:00").
const keep = (text) => text.replace(/(\d) (?=\d|h\b|kr\b|min\b)/g, '$1\u00a0').replace(/(\d)–(\d)/g, '$1\u2060–\u2060$2');

// `publicList` : the page read without signing in, from the public answers
// of the standard role; it has no way back of its own (the header's Sign in).
export default function PricesPage({ config, link, publicList = false }) {
  const [state, setState] = React.useState({ kind: 'loading' });
  React.useEffect(() => {
    let live = true;
    (async () => {
      const read = config.choices ? { ok: true, data: config.choices } : await (publicList ? readPublicChoices() : readChoices());
      if (!live) return;
      if (!read.ok) {
        if (read.kind === 'signed-out') window.location.assign('/login/');
        else setState({ kind: 'error' });
        return;
      }
      const filter = filterOf(read.data, readAddress(window.location.search));
      if (!filter) {
        setState({ kind: 'error' });
        return;
      }
      const categories = priceColumns(config.price_categories);
      const start = browserToday();
      const ask = publicList ? readPublicCalendar : readCalendar;
      const answers = await Promise.all(categories.map((row) => ask(
        priceQuery(calendarQuery({ ...filter, type: null, quantity: 1, group: null, category: row.id }), row.id, start, DAYS),
      )));
      if (!live) return;
      if (answers.some((one) => !one.ok)) setState({ kind: 'error' });
      else setState({ kind: 'ready', filter, categories, book: priceBook(answers.map((one) => one.data), categories, typeOrder(read.data)) });
    })();
    return () => { live = false; };
  }, []);
  const back = publicList ? null : link({ view: 'book' });
  const head = back && <Link href={back.href} onClick={back.onClick} variant="body2" sx={{ alignSelf: 'flex-start', color: 'text.primary' }}>{label('cart.back')}</Link>;
  if (state.kind === 'loading') {
    return <Stack spacing={2}>{head}<Box sx={{ display: 'flex', justifyContent: 'center', py: 4 }}><CircularProgress aria-label={label('common.loading')} /></Box></Stack>;
  }
  if (state.kind !== 'ready') return <Stack spacing={2}>{head}<Alert severity="error">{label('common.could-not-load')}</Alert></Stack>;
  const { filter, categories, book } = state;
  const columns = categories.length + 1;
  return <Stack spacing={2}>
    {head}
    <Box>
      <Typography component="h1" variant="h4">{label('prices.title')}</Typography>
      <Typography sx={{ color: 'text.secondary' }}>{filter.page.name}</Typography>
    </Box>
    <TableContainer sx={{ border: 1, borderColor: 'divider', borderRadius: 2, bgcolor: 'background.paper' }}>
      <Table size="small" sx={{ tableLayout: 'fixed', '& td, & th': { borderColor: 'divider' }, '& col:first-of-type': { width: { xs: '31%', sm: '24%' } } }}>
        <colgroup>
          <col />
          {categories.map((category) => <col key={category.id} />)}
        </colgroup>
        <TableHead>
          <TableRow>
            <TableCell sx={CELL}><Box component="span" sx={{ position: 'absolute', width: 1, height: 1, overflow: 'hidden', clip: 'rect(0 0 0 0)' }}>{label('prices.prices')}</Box></TableCell>
            {categories.map((category) => {
              const Icon = ICONS[categoryIcon(category.icon)];
              return <TableCell key={category.id} sx={CELL}>
                <Stack direction="row" spacing={0.75} sx={{ alignItems: 'center' }}>
                  {Icon && <Icon sx={{ fontSize: 18, color: 'text.secondary' }} />}
                  <Typography variant="body2" sx={{ fontWeight: 600 }}>{category.name}</Typography>
                </Stack>
              </TableCell>;
            })}
          </TableRow>
        </TableHead>
        {book.map((type) => {
          const TypeIcon = type.night ? BedtimeOutlined : WbSunnyOutlined;
          return <TableBody key={type.name}>
            <TableRow>
              <TableCell component="th" scope="colgroup" colSpan={columns} sx={{ ...CELL, bgcolor: 'background.default', pt: 2 }}>
                <Stack direction="row" spacing={1} sx={{ alignItems: 'center' }}>
                  <TypeIcon sx={{ fontSize: 20, color: type.night ? '#b5b3ad' : '#ffa057' }} />
                  <Typography component="h2" variant="subtitle1" sx={{ fontWeight: 600 }}>{type.name}</Typography>
                </Stack>
              </TableCell>
            </TableRow>
            {type.blocks.map((block) => <React.Fragment key={block.id}>
              <TableRow>
                <TableCell component="th" scope="row" sx={CELL}>
                  <Typography variant="body2" sx={{ fontWeight: 600 }}>{block.days}</Typography>
                  <Typography variant="body2" sx={{ color: 'text.secondary' }}>{keep(block.hours)}</Typography>
                </TableCell>
                {block.cells.map((cell) => <TableCell key={cell.category.id} sx={CELL}>
                  {cell.offered ? <>
                    {cell.lines.map((line) => <Typography key={line} variant="body2">{keep(line)}</Typography>)}
                    {cell.hours.map((line) => <Typography key={line} variant="body2" sx={{ color: 'text.secondary' }}>{keep(line)}</Typography>)}
                  </> : <Typography variant="body2" sx={{ color: 'text.secondary' }}>{label('prices.not-offered')}</Typography>}
                </TableCell>)}
              </TableRow>
              {block.examples.length > 0 && <TableRow>
                <TableCell colSpan={columns} sx={{ ...CELL, pt: 1, pb: 0, borderBottom: 0 }}>
                  <Typography variant="caption" sx={{ color: 'text.secondary' }}>{label('prices.examples')}</Typography>
                </TableCell>
              </TableRow>}
              {block.examples.map((example, index) => <TableRow key={example.hours}>
                <TableCell component="th" scope="row" sx={{ ...CELL, py: 0.75, ...(index < block.examples.length - 1 && { borderBottom: 0 }) }}>
                  <Typography variant="body2" sx={{ color: 'text.secondary' }}>
                    <Box component="span" sx={{ whiteSpace: 'nowrap' }}>{example.hours}</Box>{' · '}<Box component="span" sx={{ whiteSpace: 'nowrap' }}>{example.length}</Box>
                  </Typography>
                </TableCell>
                {example.totals.map((total, column) => <TableCell key={categories[column].id} sx={{ ...CELL, py: 0.75, ...(index < block.examples.length - 1 && { borderBottom: 0 }) }}>
                  {total
                    ? <Typography variant="body2" sx={{ fontWeight: 600, fontVariantNumeric: 'tabular-nums' }}>{keep(total)}</Typography>
                    : <Typography variant="body2" aria-label={label('prices.not-offered')} sx={{ color: 'text.secondary' }}>—</Typography>}
                </TableCell>)}
              </TableRow>)}
            </React.Fragment>)}
          </TableBody>;
        })}
      </Table>
    </TableContainer>
  </Stack>;
}
