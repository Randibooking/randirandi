// SPDX-License-Identifier: AGPL-3.0-only
import React, { forwardRef } from 'react';
import { Box, Button, ButtonBase, Stack, Typography } from '@mui/material';
import ChevronRightRounded from '@mui/icons-material/ChevronRightRounded';
import { brand, gray } from '../theme/themePrimitives';
import { moneyWords } from './add.js';
import { label } from './labels.js';
import { CART_HREF } from './places.js';

// The cart pill: on a phone a small pill above the bottom bar, on a
// wide screen a size up with the main (pale) button. "Go to cart" is a link to
// the cart page (part 3.7), kept in the page by the shell.
const YOURS = '#12a594';

function CartCount({ n, size = 26 }) {
  return <Box sx={{ minWidth: size, height: size, px: 0.5, boxSizing: 'border-box', borderRadius: '50%',
    border: `1.5px dashed ${YOURS}`, display: 'flex', alignItems: 'center', justifyContent: 'center',
    fontSize: size > 26 ? 15 : 13, fontWeight: 600 }}>{n}</Box>;
}

// Where the pill sits, so what floats above it can sit on it.
export const pillPlace = (wide) => (wide
  ? { right: 'calc(24px + env(safe-area-inset-right))', bottom: 'calc(24px + env(safe-area-inset-bottom))' }
  : { right: 'calc(12px + env(safe-area-inset-right))', bottom: 'calc(84px + env(safe-area-inset-bottom))' });

export default forwardRef(function CartPill({ cart, wide, link }, boxRef) {
  const target = link || { href: CART_HREF };
  if (!cart || !(cart.count > 0)) return null;
  const total = cart.total ? moneyWords(cart.total, cart.currency) : label('price.free');
  const place = pillPlace(wide);
  return <Box ref={boxRef} sx={{ position: 'fixed', zIndex: 'appBar', pointerEvents: 'none', ...place }}>
    {wide
      ? <Stack direction="row" spacing={1.5} sx={{
        pointerEvents: 'auto', alignItems: 'center', pl: 1, pr: 0.75, py: 0.75, borderRadius: 999,
        bgcolor: gray[800], border: `1.5px solid ${YOURS}`, boxShadow: '0 8px 22px rgba(0, 0, 0, .5)',
      }}>
        <CartCount n={cart.count} size={30} />
        <Typography sx={{ fontSize: 16, fontWeight: 600 }}>{total}</Typography>
        <Button variant="contained" {...target} endIcon={<ChevronRightRounded />} sx={{ borderRadius: 999, pl: 2 }}>
          {label('cart.pill')}</Button>
      </Stack>
      : <ButtonBase component="a" {...target} aria-label={label('cart.pill-name', { count: cart.count, total })} sx={{
        pointerEvents: 'auto', gap: 1, pl: 0.75, pr: 1, py: 0.625, borderRadius: 999,
        bgcolor: gray[800], border: `1.5px solid ${YOURS}`, boxShadow: '0 6px 18px rgba(0, 0, 0, .5)',
      }}>
        <CartCount n={cart.count} />
        <Typography sx={{ fontSize: 14, fontWeight: 600 }}>{total}</Typography>
        <Typography component="span" sx={{ display: 'flex', alignItems: 'center', fontSize: 14, fontWeight: 600,
          color: 'primary.light', ml: 0.5 }}>{label('cart.pill')}<ChevronRightRounded fontSize="small" /></Typography>
      </ButtonBase>}
  </Box>;
});
