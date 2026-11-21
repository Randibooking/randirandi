<!-- SPDX-License-Identifier: CC-BY-SA-4.0 -->
# An illustrated guide to RandiRandi

Fourteen explanations using a fictional community house. These drawings explain
booking behavior; they are not screenshots or a floor-plan feature. The prices,
dates and opening hours are examples, not product defaults.

[Back to the introduction](../README.md) · [Booking model](booking-model.md)


## 1. The things we share

An organization can offer its shared rooms, beds and equipment through one booking system. This fictional community house brings those uses together.

![RandiRandi: open-source booking, payments and administration for shared places. A cutaway of a fictional community house contains a four-bed guest room, a dining hall and laundry machines. Architectural concept artwork, not an application floor-plan feature.](images/hero.png)

[Editable illustration](images/hero.svg)


## 2. One place. Several ways to book it.

A bed and the whole room use the same physical inventory. Booking one bed leaves the other beds available but prevents an overlapping exclusive reservation of the room.

![A single fictional four-bed guest room illustrates shared inventory. Bed one is booked and beds two to four remain available for shared stays. Exclusive use of the whole room is unavailable for the same interval. Both offers refer to the same physical beds; a whole-room booking would claim all four. Concept artwork, not a product floor-plan interface.](images/shared-inventory.png)

[Editable illustration](images/shared-inventory.svg)


## 3. From available to confirmed

Choose a time, review the price and approve any required payment. Wait for the booking confirmation: approving a Vipps payment alone does not confirm the booking.

![Illustrated flow using a fictional community-house hall booking: select 18 November, 18:00 to 20:00; review two hours at NOK 150 per hour, NOK 300 total; approve the Vipps payment; then receive booking confirmation. Payment approval alone is not booking confirmation. Conceptual objects, not actual product screenshots.](images/booking-journey.png)

[Editable illustration](images/booking-journey.svg)


## 4. Your rules, made visible

A fixed session reserves the complete configured period. A flexible offer lets the member choose a start and duration within its rules; times and prices belong to the organization.

![Two illustrative hall offers on the same 14:00 to 22:00 time axis. A fixed session is booked as the complete 18:00 to 22:00 period for NOK 600. A flexible offer allows a selected 16:00 to 19:00 slot in one-hour increments; three hours at NOK 150 per hour is NOK 450. Times, limits and prices are fictional organization configuration, not product defaults.](images/time-rules.png)

[Editable illustration](images/time-rules.svg)


## 5. The whole house includes its rooms

When a house is configured as one shared space, a whole-space booking prevents overlapping room bookings. A room booking also prevents exclusive use of that space.

![A fictional community house is explicitly configured as one Space, with guest room, hall and laundry assets and an asset marked whole_space. One whole-house reservation conflicts with bookings of those assets over the same time. This illustrates configured whole-space behavior, not nested spaces or automatic discovery of rooms.](images/whole-house-includes-rooms.png)

[Editable illustration](images/whole-house-includes-rooms.svg)


## 6. Open is not always available

Opening hours and reservations answer different questions. The washer is closed at 07:00, free at 10:00 in this example, and already booked at 14:00; access and offer rules also apply.

![A fictional laundry washer opens from 08:00 until 20:00. At 07:00 it is outside opening hours. At 10:00 it is free in this example. At 14:00 it is already booked. Opening hours and actual reservations are distinct requirements; other eligibility rules may also apply.](images/open-is-not-always-available.png)

[Editable illustration](images/open-is-not-always-available.svg)


## 7. One person, different purposes

The same member may use a room for an association meeting or a private birthday. Both the member role and the offer must allow the selected purpose; administrator access is a separate permission.

![One fictional member uses the hall for an association meeting and for a private birthday on different occasions. Purposes are booking categories allowed by the member role and by the offer. A purpose is not a role. Administrator access is a separate permission.](images/one-person-different-purposes.png)

[Editable illustration](images/one-person-different-purposes.svg)


## 8. A price that follows the booking

A quote can combine billable time and requested quantity. Here, four hours × two beds × NOK 75 per bed per hour totals NOK 600; the organization sets its own rates.

![An illustrated fictional shared-room quote takes two beds for four hours at NOK 75 per bed per hour, totalling NOK 600. Requested inventory quantity and billable time are separate factors. Prices are organization configuration; this is not a standard product rate.](images/price-follows-the-booking.png)

[Editable illustration](images/price-follows-the-booking.svg)


## 9. Keep us together

A party is kept within one group of places when a suitable group fits. If no single group fits, allocation may span groups; this is shared-place allocation, not event ticketing.

![Three physical tables represent spot groups in a fictional shared-space booking offer. Table one has four free chairs, table two seats the new party of six together, and table three has four free chairs. Group allocation tries to keep the party together when possible; if no group fits, allocation can span groups. This is shared inventory, not paid event ticketing.](images/keep-us-together.png)

[Editable illustration](images/keep-us-together.svg)


## 10. An event has a place in the calendar

An event linked to an asset reserves that asset for the event. An announcement without a linked asset makes no room reservation; paid attendance is not implemented.

![An illustrated community gathering is linked to the hall, reserving that whole asset from 16:00 to 20:00. A separate announcement-only event has no linked asset and makes no reservation. Event notices and asset reservations are supported; this illustration does not depict paid event attendance or tickets.](images/event-in-the-calendar.png)

[Editable illustration](images/event-in-the-calendar.svg)


## 11. When plans change

An administrator previews the effects before applying changed opening hours. A partial overlap with removed time cancels the entire affected booking, while a refund can remain pending until the provider completes it.

![A fictional washer needs maintenance from 10:00 to 11:00. An administrator previews and applies opening hours that now start at 11:00. An existing paid 10:00 to 12:00 booking partly overlaps the removed time, so the whole affected booking is cancelled. A NOK 300 refund is shown pending; cancellation does not prove that the provider has completed the refund.](images/when-plans-change.png)

[Editable illustration](images/when-plans-change.svg)


## 12. A calm handover

A new administrator needs to understand the places, opening hours, purposes and prices. A practice booking in a fictional organization checks that access, price and confirmation work together.

![A physical handover binder for a fictional community house contains a floor plan, opening hours, purposes and rates. A separate practice sheet reminds the new administrator to exercise the configured offer in a practice organization before changing live bookings. This is conceptual artwork, not an invented Admin interface or automated onboarding flow.](images/a-calm-handover.png)

[Editable illustration](images/a-calm-handover.svg) · [See the actual administrator view](product-walkthrough.md#the-administrators-view)


## 13. Configuration you can carry

Export and load a setup to recreate its configuration elsewhere. A setup file contains no members, bookings, payments or secrets, so it is not a database backup.

![An authentic but incomplete format 3 TOML excerpt describes a fictional community house: organization name, subdomain, time zone and currency. Floor plans on either side represent recreating configuration through export and load. Setup export contains no members, bookings, payments or secrets and is not a personal-data or database backup.](images/configuration-you-can-carry.png)

[Editable illustration](images/configuration-you-can-carry.svg)


## 14. Remember what was actually booked

Changing a rate does not rewrite the price facts stored on an existing booking. The retained booking keeps its original amount; configured retention still applies.

![A retained fictional booking receipt keeps its hall booking for two hours at NOK 150 per hour, NOK 300 total. A new price sheet sets NOK 200 per hour for future quotes. Stored booking lines retain their captured price facts through configuration edits. These records remain subject to configured retention; this is not a claim of unlimited audit history.](images/remember-what-was-booked.png)

[Editable illustration](images/remember-what-was-booked.svg)
