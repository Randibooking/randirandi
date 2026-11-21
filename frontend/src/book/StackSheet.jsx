// SPDX-License-Identifier: AGPL-3.0-only
import React from 'react';
import {
  ButtonBase, Stack, Typography,
} from '@mui/material';
import { gray } from '../theme/themePrimitives';
import { quantityOf } from './add.js';
import { Sheet } from './AddSheet';
import { bookingPriceWords } from './day.js';
import { label } from './labels.js';
import { choiceHours, clockOf, offersAt } from './stack.js';
import { dateWords } from './words.js';

function Offer({ offer, date, filter, onPick }) {
  const { container, choice } = offer;
  if (!choice) {
    const { reason } = offer;
    return <Stack role="group" aria-disabled="true" aria-label={`${container.type.name}, ${reason}`} direction="row"
      sx={{ justifyContent: 'space-between', gap: 2, py: 1, px: 1.25, border: 1, borderColor: gray[700], borderRadius: 1.5 }}>
      <Stack>
        <Typography sx={{ fontWeight: 600, opacity: 0.5 }}>{container.type.name}</Typography>
        <Typography variant="body2" sx={{ color: 'text.secondary' }}>{reason}</Typography>
      </Stack>
    </Stack>;
  }
  const hours = choiceHours(choice);
  // From the calendar's prices, so opening the stack asks the server nothing;
  // the add sheet asks the server's price for the offer picked (part 3.98).
  const price = bookingPriceWords(container, filter.category, quantityOf(container, filter), choice.start.at, choice.end,
    filter.group);
  return <ButtonBase component="button" onClick={() => onPick(container, choice.start.at)}
    aria-label={`${container.type.name}, ${dateWords(date)}, ${hours}${price ? `, ${price}` : ''}`}
    sx={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 2, py: 1, px: 1.25, textAlign: 'left',
      border: 1, borderColor: gray[600], borderRadius: 1.5, width: '100%' }}>
    <Stack>
      <Typography sx={{ fontWeight: 600 }}>{container.type.name}</Typography>
      <Typography variant="body2" sx={{ color: 'text.secondary' }}>{hours}</Typography>
    </Stack>
    <Typography sx={{ fontWeight: 600, whiteSpace: 'nowrap' }}>{price}</Typography>
  </ButtonBase>;
}

// "From 17:00 you can book": every day-time offer of the asset at the hour.
export default function StackSheet({ wide, asset, containers, date, minute, filter, onClose, onPick }) {
  const offers = React.useMemo(() => offersAt(containers, date, minute), [containers, date, minute]);
  const title = <Stack spacing={0.25}>
    <Typography variant="h6" sx={{ fontWeight: 600 }}>{label('stack.title', { time: clockOf(minute) })}</Typography>
    <Typography variant="body2" sx={{ color: 'text.secondary' }}>{`${asset.name}, ${dateWords(date)}`}</Typography>
  </Stack>;
  return <Sheet wide={wide} onClose={onClose} title={title}>
    <Stack spacing={1}>
      {offers.map((offer) => <Offer key={offer.container.id} offer={offer} date={date} filter={filter} onPick={onPick} />)}
    </Stack>
  </Sheet>;
}
