<!-- SPDX-License-Identifier: CC-BY-SA-4.0 -->
# Booking model

RandiRandi separates what exists from how people may book it. Organization
configuration supplies names, opening hours, roles and prices. The rules below
apply to every organization.

## Asset → container → grid

An **asset** is a physical thing: a room, machine, sauna or hall. A **container**
is one offer on that asset, with a booking type, inventory, recurrence, time
rules and payment choice. A **grid** is a dated occurrence of that offer,
computed for the calendar. Several containers can offer the same asset.

| Object | Responsibility | Example |
| --- | --- | --- |
| Space | Physical location shared by assets | Community house |
| Asset | Availability and physical inventory | Guest room |
| Floor plan | One arrangement of places in the asset | Four beds |
| Spot | One unit of that inventory, named or unnamed | Bed A1 |
| Spot group | A subset used for selection and group prices | Room A |
| Booking type | The member's first kind of choice | Overnight stay |
| Booking category | Purpose allowed to roles and offers | Private use |
| Role | Category access and separate Admin permission | Member |
| Container | The actual bookable offer | One bed, Sunday–Thursday |
| Price book | Prices attached to offers and categories | Member rates |

A group is not an independent inventory or a reservation of all its places.
A shared booking takes actual spots. An exclusive booking takes the whole
asset. A marked whole-space asset also conflicts with assets in that space.

## Availability and periods

Weekly availability says when the asset can be used. Dated calendars and
closures restrict that availability. A period over midnight needs continuous
availability on both days. An interval is half-open: a booking may start when
another ends.

Containers have fixed or flexible periods. Fixed offers use a defined start
and end; flexible offers use an interval and duration bounds. Recurrence may
restrict weekdays, first/last calendar weeks and every N weeks. Category
hours can further restrict a category within the container's hours.

A container's own repeating periods must not overlap. Shared containers on
different floor plans of one asset cannot cover the same time. Other containers
may overlap in the offer; checkout resolves competition for the same physical
inventory. Disabling an offer stops new choices, not existing reservations.

Booking dates and times use the asset's time zone, which defaults to the
organization's zone. Organization-wide daily booking limits use the organization's
zone. Stored instants are aware times; clock changes can change elapsed duration.
The calendar, price calculation and checkout apply the same supported bounds.

## Eligibility and prices

The member's active status, role, category access, booking limits, asset
availability, category hours, inventory and price coverage all affect a choice.
Administrator access is not ordinary-member eligibility.

Payment-free offers require confirmation but have no price book. Paid offers
link a price book. Entries apply to a container/category, with an optional
spot-group price. Pricing may be per booking or per spot, with supported
quantity/duration or time-of-day tiers. Missing price coverage makes that
combination unavailable. Group-specific prices need the category's base price.

The member calendar is an offer, not a reservation. Checkout recalculates the
price and availability under transaction locks. It refuses stale or conflicting
choices without partially accepting the cart.

## Orders, payment and cancellation

An order contains booking lines. Checkout creates a bounded hold while payment
is arranged. Vipps authorization or capture alone is not proof that the booking
was confirmed. Provider callbacks and reconciliation update the same order;
retries must not create another payment or reservation.

Confirmed bookings occupy their inventory throughout the interval. Expired or
cancelled holds release it. Cancellation and refund are distinct: an
administrator can cancel a whole booking or an eligible line and review the
refund choice. Refund work may be queued; its result must be checked.

Payment/refund history retains monetary totals needed after old booking rows
have reached retention. Captured money on an unconfirmed order requires
financial follow-up, rather than ordinary deletion.

## Events and operational blocks

Events publish information and may reserve an asset interval. A recurring
series can have dated occurrences changed or removed. Paid attendance is not
available. Operational blocks take a timed interval out of booking; closures
cover whole dates in the asset's availability. Existing affected bookings need
the explicit cancellation/refund process offered by the relevant Admin action.

See [Data model](data-model.md) for storage and [Setup files](setup-file.md)
for portability. Tests in `tests/` exercise model, calendar, pricing,
concurrency, checkout and provider boundaries together.
