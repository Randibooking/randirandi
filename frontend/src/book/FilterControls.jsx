// SPDX-License-Identifier: AGPL-3.0-only
import React from 'react';
import {
  Box, Button, Chip, Dialog, Drawer, IconButton, Stack, Tab, Tabs, ToggleButton, ToggleButtonGroup, Typography,
} from '@mui/material';
import AddRounded from '@mui/icons-material/AddRounded';
import ApartmentRounded from '@mui/icons-material/ApartmentRounded';
import CloseRounded from '@mui/icons-material/CloseRounded';
import GroupsRounded from '@mui/icons-material/GroupsRounded';
import HomeRounded from '@mui/icons-material/HomeRounded';
import PersonRounded from '@mui/icons-material/PersonRounded';
import RemoveRounded from '@mui/icons-material/RemoveRounded';
import TuneRounded from '@mui/icons-material/TuneRounded';
import WorkRounded from '@mui/icons-material/WorkRounded';
import { gray } from '../theme/themePrimitives';
import { BookShown } from './AddSheet';
import { categoryIcon, CALENDAR_ID, categoryTabId } from './categories.js';
import { label, plural } from './labels.js';

// FilterBar, FilterSheet and Sheet; the sheet is a MUI Drawer on a phone.
const nameOf = (rows, id) => rows.find((row) => row.id === id)?.name ?? '';

const chipX = { '& .MuiChip-deleteIcon': { fontSize: 16, color: 'text.secondary', '&:hover': { color: 'text.primary' } } };

// The Filter button and, beside it, what is chosen: never empty. A choice that
// can be taken away has an x. The booking category is not here: it is the
// page's own switch above it (BookingAs).
export function FilterBar({ filter, onOpen, onChange }) {
  const chips = [];
  const add = (key, name, patch) => chips.push(
    <Chip key={key} size="small" label={name} onClick={onOpen}
      onDelete={() => onChange(patch)} deleteIcon={<CloseRounded aria-label={label('filter.remove', { name })} />} sx={chipX} />,
  );
  if (filter.assets.length && filter.asset !== filter.assets[0].id) {
    add('asset', nameOf(filter.assets, filter.asset), { asset: filter.assets[0].id });
  }
  if (filter.type != null) add('type', nameOf(filter.types, filter.type), { type: null });
  if (filter.quantity !== 1) add('quantity', plural('filter.quantity-chip', filter.quantity, { word: filter.spotWord || label('spot.word') }), { quantity: 1 });
  if (filter.group != null) add('group', nameOf(filter.groups, filter.group), { group: null });
  return <Stack direction="row" sx={{ gap: 1, flexWrap: 'wrap', alignItems: 'center' }}>
    <Button variant="outlined" size="small" startIcon={<TuneRounded />} onClick={onOpen}>{label('filter.button')}</Button>
    {chips}
  </Stack>;
}

// The icon of each key a category may have (`CATEGORY_ICONS` in
// randirandi/limits.py; categories.test.js holds the two lists together).
const ICONS = {
  person: PersonRounded, work: WorkRounded, building: ApartmentRounded, home: HomeRounded, group: GroupsRounded,
};

// What the member books as: the page's own switch under the asset's name,
// above the Filter, every choice in sight . Shown with more than one category.
// Arrow keys move between the tabs and Enter or Space chooses, as the change
// is not made by passing over a tab. Each tab controls the calendar region.
export function BookingAs({ filter, wide, onChange }) {
  const caption = React.useId();
  if (filter.categories.length < 2) return null;
  return <Box sx={{ borderBottom: 1, borderColor: 'divider' }}>
    <Typography id={caption} variant="caption" sx={{ color: 'text.secondary' }}>{label('filter.booking-as')}</Typography>
    <Tabs value={filter.category} variant={wide ? 'standard' : 'fullWidth'} aria-labelledby={caption}
      onChange={(_, category) => onChange({ category }, true)} sx={{ mt: 0.5 }}>
      {filter.categories.map((row) => {
        const Icon = ICONS[categoryIcon(row.icon)];
        return <Tab key={row.id} value={row.id} label={row.name} id={categoryTabId(row.id)} aria-controls={CALENDAR_ID}
          icon={Icon ? <Icon /> : undefined} iconPosition="start"
          sx={{ fontSize: 14, fontWeight: 600, px: 1.5, py: 0.75, minHeight: 44, '& .MuiTab-icon': { fontSize: 18 } }} />;
      })}
    </Tabs>
  </Box>;
}

// A dialog on a wide screen, a bottom Drawer on a phone. The dialog is the
// cards' surface: MUI lightens a raised dialog in the dark.
export function Sheet({ open: asked, onClose, phone, children }) {
  const shown = React.useContext(BookShown);
  const open = asked && shown;
  const title = React.useId();
  const head = <Stack direction="row" sx={{ alignItems: 'flex-start', mb: 1.5 }}>
    <Typography id={title} variant="h6" sx={{ fontWeight: 600, flex: 1 }}>{label('filter.title')}</Typography>
    <IconButton size="small" onClick={onClose} aria-label={label('common.close')}><CloseRounded fontSize="small" /></IconButton>
  </Stack>;
  if (!phone) {
    return <Dialog open={open} onClose={onClose} aria-labelledby={title} fullWidth maxWidth="xs"
      slotProps={{ paper: { sx: { bgcolor: 'background.paper', backgroundImage: 'none' } } }}>
      <Box sx={{ p: 3 }}>{head}{children}</Box>
    </Dialog>;
  }
  // The Drawer's paper is the dialog, so its name goes there.
  return <Drawer anchor="bottom" open={open} onClose={onClose}
    slotProps={{ paper: { 'aria-labelledby': title, sx: {
      bgcolor: 'background.paper', backgroundImage: 'none', borderTop: 1, borderColor: 'divider',
      borderRadius: '18px 18px 0 0', p: 2.5, pb: 'calc(24px + env(safe-area-inset-bottom))', maxHeight: '88%',
    } } }}>
    <Box sx={{ width: 40, height: 4, borderRadius: 2, bgcolor: gray[600], mx: 'auto', mb: 1.5 }} />
    {head}
    {children}
  </Drawer>;
}

// The Filter's rows of choices sit flat, as the Month and Week switch beside it
// does; the theme's shadow under each choice made them look like raised keys.
const choices = { mt: 0.5, boxShadow: 'none', '& .MuiToggleButton-root': { boxShadow: 'none', py: 0.75 } };

// One row of choices (). More than three names would not fit a phone's
// row, so they stand one under the other; more than five numbers in order
// (how many) are a stepper.
export function Row({ caption, value, options, onPick, ordered = false }) {
  const at = options.findIndex(([key]) => key === value);
  const heading = <Typography variant="caption" sx={{ color: 'text.secondary' }}>{caption}</Typography>;
  if (ordered && options.length > 5) {
    return <Box>
      {heading}
      <Stack direction="row" role="group" aria-label={caption} sx={{ mt: 0.5, alignItems: 'center', gap: 1 }}>
        <IconButton size="small" aria-label={label('filter.fewer', { caption })} disabled={at <= 0}
          onClick={() => onPick(options[at - 1][0])}><RemoveRounded fontSize="small" /></IconButton>
        <Typography aria-live="polite" sx={{ minWidth: 72, textAlign: 'center' }}>{options[at]?.[1]}</Typography>
        <IconButton size="small" aria-label={label('filter.more', { caption })} disabled={at >= options.length - 1}
          onClick={() => onPick(options[at + 1][0])}><AddRounded fontSize="small" /></IconButton>
      </Stack>
    </Box>;
  }
  return <Box>
    {heading}
    <ToggleButtonGroup exclusive size="small" fullWidth value={value} aria-label={caption} sx={choices}
      orientation={!ordered && options.length > 3 ? 'vertical' : 'horizontal'}
      onChange={(_, next) => next !== null && onPick(next)}>
      {options.map(([key, words]) => <ToggleButton key={key} value={key}>{words}</ToggleButton>)}
    </ToggleButtonGroup>
  </Box>;
}

// Each part shows only when there is something to choose; every part is the
// same row of choices, asked in the member's words.
export function FilterSheet({ open, phone, filter, onChange, onClose }) {
  return <Sheet open={open} phone={phone} onClose={onClose}>
    <Stack spacing={2}>
      {filter.types.length > 0 && <Row caption={label('filter.type')} value={filter.type ?? 'all'}
        options={[['all', label('filter.type-all')], ...filter.types.map((row) => [row.id, row.name])]}
        onPick={(type) => onChange({ type: type === 'all' ? null : type })} />}
      {filter.assets.length > 0 && <Row caption={label('filter.asset')} value={filter.asset}
        options={filter.assets.map((row) => [row.id, row.name])}
        onPick={(asset) => onChange({ asset })} />}
      {filter.quantities.length > 0 && <Row caption={label('filter.quantity')} value={filter.quantity}
        options={filter.quantities.map((n) => [n, n])} ordered
        onPick={(quantity) => onChange({ quantity })} />}
      {filter.groups.length > 0 && <Row caption={label('filter.group')} value={filter.group ?? 'none'}
        options={[['none', label('filter.group-none')], ...filter.groups.map((row) => [row.id, row.name])]}
        onPick={(group) => onChange({ group: group === 'none' ? null : group })} />}
      <Button variant="contained" onClick={onClose}>{label('filter.show')}</Button>
    </Stack>
  </Sheet>;
}
