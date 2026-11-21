// SPDX-License-Identifier: AGPL-3.0-only
import React from 'react';
import { Box, Divider, IconButton, Stack, Typography } from '@mui/material';
import BedtimeOutlined from '@mui/icons-material/BedtimeOutlined';
import WbSunnyOutlined from '@mui/icons-material/WbSunnyOutlined';
import DeleteOutlineRounded from '@mui/icons-material/DeleteOutlineRounded';
import { brand, orange } from '../theme/themePrimitives';
import { lineView, manyAssets, onlyLine, sumWords } from './cart.js';
import { label } from './labels.js';

// The lines grouped by booking type, ported from the operator's demo
// (`ItemRow`, `ItemGroups`): a heading per type with its icon, count and sum,
// under it each line with its date, time, what it is booked as and its price.
// `groups` is `groupLines`; `onRemove` is given only in the cart.
export default function LineGroups({ groups, currency, onRemove, busy = false }) {
  const withAsset = manyAssets(groups);
  const single = onlyLine(groups);
  return <Stack spacing={2}>
    {groups.map((group) => {
      const Icon = group.night ? BedtimeOutlined : WbSunnyOutlined;
      return <Box key={group.type}>
        <Stack direction="row" spacing={1} sx={{ alignItems: 'center', mb: 0.75, px: 0.5 }}>
          <Icon sx={{ fontSize: 20, color: group.night ? brand[200] : orange[300] }} />
          <Typography component="h2" sx={{ fontWeight: 600, flex: 1 }}>{group.type}
            <Box component="span" sx={{ color: 'text.secondary', fontWeight: 400 }}> · {group.count}</Box></Typography>
          {!single && <Typography sx={{ fontWeight: 600 }}>{sumWords(group.sum, currency)}</Typography>}
        </Stack>
        <Box sx={{ border: 1, borderColor: 'divider', borderRadius: 3, px: 2, bgcolor: 'background.paper' }}>
          {group.lines.map((line, index) => {
            const view = lineView(line, currency, Boolean(onRemove), withAsset);
            return <React.Fragment key={line.id ?? `${group.type}|${line.start}|${index}`}>
              {index > 0 && <Divider />}
              <Stack direction="row" spacing={1.5} sx={{ py: 1.25, alignItems: 'flex-start' }}>
                <Box sx={{ flex: 1, minWidth: 0 }}>
                  <Typography sx={{ fontWeight: 600 }}>{view.date}</Typography>
                  <Typography variant="body2" sx={{ color: 'text.secondary' }}>{view.time}</Typography>
                  <Typography variant="caption" sx={{ color: 'text.secondary', display: 'block' }}>
                    {[view.asset, view.bookedAs, view.places].filter(Boolean).join(' · ')}</Typography>
                  {view.marked && <Typography variant="caption" sx={{ color: 'error.light', display: 'block' }}>{view.marked}</Typography>}
                </Box>
                <Typography sx={{ fontWeight: 600, whiteSpace: 'nowrap' }}>{view.price}</Typography>
                {onRemove && <IconButton size="small" disabled={busy}
                  aria-label={label('cart.remove-line', { line: [group.type, view.asset, view.date, view.time].filter(Boolean).join(', ') })}
                  onClick={() => onRemove(line)}><DeleteOutlineRounded fontSize="small" /></IconButton>}
              </Stack>
            </React.Fragment>;
          })}
        </Box>
      </Box>;
    })}
  </Stack>;
}
