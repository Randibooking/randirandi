<!-- SPDX-License-Identifier: CC-BY-SA-4.0 -->
# Organization setup files

A setup is portable configuration, written as UTF-8 TOML. It contains the
organization, labels, roles, assets, availability, offers and prices. It
contains no members, bookings, payments, provider credentials or audit history.
Use a protected organization contact address, not a personal member register.

The reader accepts formats 1 and 3 and refuses format 2. Format 1 retains the
exclusive baseline and its original defaults. Format 3 supports spaces, floor
plans, shared spots/groups and closures. The writer always exports format 3.
A request to write format 1 is refused if it would silently discard configuration.

The fictional examples in `setups/` exercise the format. Start with
`setups/example.toml` for an exclusive offer; use a scenario for shared places.
Configuration validation uses the same general rules as the Admin screens.

## The file

Write one UTF-8 TOML file per organization. The loader accepts a file path or
standard input. Fictional examples live in `setups/` and `setups/scenarios/`.

TOML was chosen because Python reads it without an added package, and
because a text in it is always in quotes: a time such as `"17:00"` and a
word such as `"no"` are read exactly as they are written.

How values are written:

| Kind | Written as | Example |
|---|---|---|
| Text | In quotes | `"Guest room"` |
| Whole number | Digits | `36` |
| Yes or no | `true` or `false` | `false` |
| Date | Year, month, day, without quotes | `2027-01-31` |
| Time of day | Hours and minutes, in quotes | `"17:00"` |
| Length | A whole number and its unit, `h` or `min`, in quotes | `"4 h"`, `"90 min"` |
| Amount | In the currency's main unit, without quotes | `250`, `249.50` |
| Weekday | Three letters | `"mon"`, `"tue"`, `"wed"`, `"thu"`, `"fri"`, `"sat"`, `"sun"` |
| List | In square brackets | `["private", "work"]` |

A length always carries its unit, so 4 hours cannot be mistaken for 4
minutes. An amount is written the way a price list writes it, kr 250 as
`250`; the service stores it in the currency's minor unit.

The file starts with the version of the format:

```toml
format = 3
```

A file in format 1 starts with `format = 1`.

## The sections

Each section fills in one object of [Data model](data-model.md), and each
field has the name it has there, except where the table says otherwise. A
repeated section uses its name in double square brackets, once for each
object. An empty required collection is written as `assets = []`, for
example, at the top level before any table. `spaces` and `labels` may be
left out entirely. The tables are format 3; what format 1 leaves out is said with
each.

### `[organization]`

| Field | Value | Required | Notes |
|---|---|---|---|
| `name` | text | yes | |
| `subdomain` | text | yes | The first part of the address its members use. The deployment appends `RANDIRANDI_ORGANIZATION_DOMAIN`, for example `harbourview.example.org`. The data model stores it as written here |
| `time_zone` | text | yes | For example `"Europe/Oslo"`. The zone an asset takes when it names none |
| `currency` | text | yes | One of `"NOK"`, `"DKK"` and `"EUR"`, the currencies the payment client takes. The currency a price book takes when it names none |
| `contact_email` | text | yes | The organization's address wherever a member sees one: the bar at the top of every page, the contact on another member's booking, where a request to change a booking goes, the Reply-To of the booking mails and the footer of every mail. One plain address; an administrator changes it in Admin, Organization |
| `standard_role` | text | yes | The name of the role a member import gives a new member |
| `member_retention_months` | whole number | yes | |
| `booking_retention_months` | whole number | yes | |
| `max_bookings_a_day` | whole number, 1 or more | no | The most bookings one member may make in one calendar day on the organization's clock (`time_zone`), counted by the day they are ordered. Left out: no limit |
| `book_ahead_months` | whole number, 1 or more | no | How far ahead a member may book, in calendar months from today; the last bookable date is included whole. Left out: no limit |
| `tax_rate` | whole number, 0 to 10000 | no | The tax in the prices, in hundredths of a percent: `2500` is 25 %. It goes on the receipt in the Vipps app. Left out: none (0). An older reader refuses a file that has it |
| `language` | text | no | The language of the organization's pages and mails: a code whose catalogue the code ships, `randirandi/locale/<code>.json`; any other code is refused. Left out: `"en"`. An older reader refuses a file that has it |
| `free_cancellation_days` | whole number, 0 to 365 | no | The days before a booked time starts until which a member who asks by email to cancel it gets back what was paid for it: the booking conditions' `{days}`. Left out: 5. An older reader refuses a file that has it |

### `[labels]`, optional (format 3)

The organization's own words on the member pages. Every word
those pages show is a label: a key and its default English text, all in one
file per language, [`randirandi/locale/en.json`](../randirandi/locale/en.json) for
English, the product's own language, which holds every key. This table gives
the organization's text, in its `language`, for the keys it wants to say its
own way; every key left out keeps its language's catalogue text (English where
the catalogue lacks the key). The keys are the file's, in quotes because they
hold dots: `"place.book" = "Reserve"`.

| Part | Value | Notes |
|---|---|---|
| key | a label key | One of the keys in `randirandi/locale/en.json`. An unknown key is refused |
| text | text, 1 to 200 characters | No space at either end. It uses exactly the placeholders of the key's text in the language's catalogue: a text `Needs {length} free from {time}` is answered by a text that holds `{length}` and `{time}` (each as often as it likes, in any order), and a default without placeholders by a text without. A placeholder stands for a value the page fills in |

```toml
[labels]
"place.book" = "Reserve"
"stack.needs" = "At {time} you need {length} free"
```

A word is never made from another by grammar: the page adds nothing to a
label, and nothing to a name the organization gave (a spot word such as
"seat" or "bed" goes in as written). A label is phrased around its
placeholders, so write a plural, a case or an article into the text itself.
The word for the spots a booking takes is not a label: it is the floor
plan's own `spot_word`, which the booking answers carry. The refusal and
order-state keys (`taken`, `price-changed`, `held`, …) are labels too, with
the server's default text; the page shows the organization's text for them.
Login texts also use the language catalogue and organization label overrides. Admin controls currently remain English.

A load writes the table to the organization (`Organization.labels`), and an
export writes back only the keys that are set, in the order of the label
file, so that a file reads back as the same setup. The member pages get the
words of the organization's language and its own words, resolved
(`ui.labels` in the page's config); the English defaults are in the page's code.

### `[[spaces]]`, zero or more (format 3)

| Field | Value | Required | Notes |
|---|---|---|---|
| `number` | whole number | yes | Positive, unique per organization |
| `name` | text | yes | Unique among spaces |
| `description` | text | no | |
| `booking_page` | `"one_asset"` or `"whole_space"` | no, default `"one_asset"` | Whether the space's booking page shows one asset at a time or the whole space, every asset's containers side by side |

A space is a grouping of assets and reserves nothing. It can exist without
an asset in it. Nested spaces are not supported; there is no `inside` key.

### `[[assets]]`, zero or more

| Field | Value | Required | Notes |
|---|---|---|---|
| `number` | whole number | yes | Positive, unique per organization |
| `name` | text | yes | The booking page's heading |
| `description` | text | no | Shown under the name when written: what a member needs to know, never a second name |
| `space` | text | no | The name of its space. Not in format 1 |
| `whole_space` | yes or no | no, default `false` | The mark "covers its whole space"; it needs a space. Not in format 1 |
| `maximum_occupancy` | whole number | no | How many people the law allows there, 1 or more: information only, never checked against a floor plan. Not in format 1 |
| `time_zone` | text | no; format 3 only | The zone its containers, hours and prices by time of day are read in and members see its times in, such as `"America/New_York"`. Left out, the organization's. The assets of one space share one zone (the file is refused otherwise). The export writes it only when it differs from the organization's, so a file without it exports without it |

An asset marked as covering its whole space (`whole_space = true`, the name
holds) has its bookings kept apart from those of every asset in its
space by the database; its containers may stack with theirs. One asset per
space carries the mark: a second is refused (`this space already has an
asset that covers it`); different whole-space offers are its containers. An
asset without a space cannot carry it.

### `[[assets.floor_plans]]`, under its asset, zero or more (format 3)

| Field | Value | Required | Notes |
|---|---|---|---|
| `name` | text | when the asset has more than one | Unique among the asset's floor plans. A shared container names it |
| `spot_word` | text | yes | The word for its spots, such as `"seat"`, `"bed"` or `"machine"` (stored as `unit`, a database name) |
| `spots` | whole number, or list of texts | yes | The spots, as their number (`200`) or as their names in the order of their numbers (`["Bed 1", "Bed 2"]`), with `""` for a spot without a name. A name is given once. At most 10,000 spots |

Spots are numbered from 1 in the order written. An asset the file gives no
floor plan gets one of one spot without a name, spot word `booking`, as in
format 1; an export leaves that floor plan out again. Floor plans currently
describe inventory, not a visual seating editor.

### `[[assets.floor_plans.groups]]`, under its floor plan, zero or more

| Field | Value | Required | Notes |
|---|---|---|---|
| `name` | text | yes | Unique within the floor plan |
| `word` | text | no, default `"group"` | The word for the group, such as `"table"`, `"section"` or `"row"` |
| `spots` | list of spot numbers | yes | Spots of this floor plan, each in at most one group. May be empty |

Groups are numbered from 1 in the order written. A group is never booked.
When categories of seats cut across rows, the groups are the categories and
the row goes in the spot's name (`"A1"`).

Space names and descriptions, floor plan names, spot words, spot names and
group names and words keep model-valid whitespace exactly, including
multiline descriptions; a reference matches the exact name, and whitespace
is never silently stripped. Other texts may not start or end with a space.

A booking takes spots of a floor plan. A named
bed is a spot with a name, never an asset of its own. Two machines in one
room are two assets in one space, each with a floor plan of one spot; four
beds sold by the bed or as the whole room are one asset with a floor plan
of four spots. `setups/scenarios/` contains complete runnable examples.

### `[[availability_calendars]]`, zero or more

| Field | Value | Required | Notes |
|---|---|---|---|
| `asset` | text | yes | The name of the asset |
| `start_date` | date | no | Left out, the calendar applies from the day the setup is loaded |
| `spans` | list | yes | The available spans, see below |

One available span:

| Field | Value | Required | Notes |
|---|---|---|---|
| `weekdays` | list of weekdays | yes | One line may name several weekdays. It is the same as one span for each |
| `start` | time of day | yes | |
| `end` | time of day | yes | May be `"24:00"` |

A weekday that no span names is not available.

Outside asset availability nothing is bookable. In Admin, an availability
change or dated closure previews affected bookings before cancelling them and
arranging refunds. A pending refund is not a completed refund. Loading a setup
creates a new organization; it cannot update an existing one.

### `[[closures]]`, zero or more (format 3)

A date, or a run of dates, closed beside an asset's weekly availability. Whole days on the asset's clock.

| Field | Value | Required | Notes |
|---|---|---|---|
| `asset` | text | yes | The name of the asset |
| `first_date` | date | yes | `2027-12-24`, 2000 or later |
| `last_date` | date | yes | Not before the first date; 2100 at the latest. For a single day the same date |
| `reason` | text | no | `"holiday"`, `"repair"` or `"vacation"`. Left out, none |

Format 1 refuses the table. An export writes the closures in date order; the file reloads to the
same setup.

### `[[booking_types]]`, zero or more

| Field | Value | Required | Notes |
|---|---|---|---|
| `name` | text | yes | |
| `description` | text | no | |

The order in the file is the order members choose from. It is the position
of the data model.

### `[[booking_categories]]`, one or more

| Field | Value | Required | Notes |
|---|---|---|---|
| `name` | text | yes | |
| `icon` | `person`, `work`, `building`, `home` or `group` | no | The icon at the start of the category's tab on the booking page. Left out, there is none; an export writes it only when set |

The order in the file is their position in lists.

### `[[roles]]`, one or more

| Field | Value | Required | Notes |
|---|---|---|---|
| `name` | text | yes | |
| `booking_categories` | list of names | yes | |
| `admin_access` | yes or no | yes | |

### `[[booking_configurations]]`, zero or more

The table describes format 3. Format 1 has no consumption mode or floor plan:
every container is exclusive.

| # | Field | Value | Required | Notes |
|---|---|---|---|---|
| 1 | `number` | whole number | yes | Positive, unique per organization |
| 2 | `asset` | text | yes | The name of the asset |
| 3 | `booking_type` | text | yes | The name of the booking type |
| 4 | `period_type` | `"fixed"` or `"flexible"` | yes | |
| 5 | `period_start` | time of day | yes | |
| 6 | `period_end` | time of day | yes | Midnight is `"00:00"` with an end day of 1 |
| 7 | `end_day` | whole number | yes | 0 is the same day, 1 the next day |
| 8 | `period_start_weekdays` | list of weekdays | yes | |
| 9 | `interval` | length | flexible only | For a fixed period the system sets it, and the file leaves it out |
| 10 | `minimum_length` | length | no; flexible only | Left out, the system sets it to the interval. For a fixed period, as above |
| 11 | `maximum_length` | length | flexible only | As above |
| 12 | `start_week` | calendar week, `"2027-W01"` | no, but needed when `every_weeks` is above 1 | The first calendar week a period may start in. Left out: from always |
| 13 | `stop_week` | calendar week, `"2027-W52"` | no | The last calendar week a period may start in. Left out: without end |
| 14 | `status` | `"enabled"` or `"disabled"` | yes | |
| 15 | `price_book` | text | yes with payment; format 1 always | The name of the price book. Format 3 may leave it out: the container is then without payment, no price applies to it, and its bookings are confirmed without a payment |
| 16 | `booking_categories` | list of names | yes | |
| 17 | `consumption_mode` | `"exclusive"` or `"shared"` | yes; not in format 1 | Exclusive takes the whole asset; shared takes spots of one of its floor plans |
| 18 | `every_weeks` | whole number, 1 to 52 | no, default 1 | A period starts in every Nth calendar week from `start_week`, counted straight across New Year |
| 19 | `floor_plan` | text | shared only; not in format 1 | The name of the asset's floor plan the container books. May be left out when the asset has one floor plan. An exclusive container names none |
| 20 | `category_hours` | list of `{ booking_category, weekdays, start, end }` | no | A booking category's own hours on this container, inside its hours, like an availability calendar's `spans`. A category with no row has the container's hours; once it has one, a weekday without a row is closed to it. Each `booking_category` is one the container names. Both formats read it |

Fields 12, 13 and 18 are calendar weeks : ISO weeks, a week from
Monday to Sunday; a period that runs into the next week belongs to the week
it starts in. The file holds weeks, the database the Monday of the first
and the Sunday of the last (`start_date`, `stop_date`, database names that
hold). A configuration's `start_date` or `stop_date` key is refused with a
message naming the week key.
Field 15 may be left out in format 3 for a container without
payment; format 1 still needs it on every container
(`booking_configurations[1].price_book: is missing: format 1 has no
container without payment; use format 3.`).

### `[[price_books]]`, zero or more

| Field | Value | Required | Notes |
|---|---|---|---|
| `number` | whole number | yes | Positive, unique per organization |
| `name` | text | yes | |
| `currency` | text | no; format 3 only | What its amounts are in: `"NOK"`, `"DKK"` or `"EUR"`. Left out, the organization's. The amounts of its entries are written in this currency's main unit. The export writes it only when it differs from the organization's |

Its entries follow it, each under `[[price_books.entries]]`. An entry is one
cell of the grid: one booking configuration in one booking category.

| Field | Value | Required | Notes |
|---|---|---|---|
| `booking_configuration` | whole number | yes | The configuration's number. It must be a configuration that links to this price book |
| `booking_category` | text | yes | One of that configuration's booking categories |
| `tiers_go_by` | `"length"` or `"time of day"` | yes | |
| `billing_basis` | `"per_booking"` or `"per_capacity_unit"` | yes; not in format 1 | Per booking, or per spot; a price per spot is for a shared container only. Format 1 means per booking |
| `group` | text | no; format 3 only | The name of a group of spots on the floor plan the configuration books: this entry is then the price of a spot in that group . Left out, the entry is the configuration's normal price, which a spot in no group, or in a group without a price of its own, takes. A price for a group is per spot (`billing_basis = "per_capacity_unit"`), and so is the normal price beside it |
| `tiers` | list | yes | One or more price tiers, in order |

One price tier:

| Field | Value | Required | Notes |
|---|---|---|---|
| `up_to` | length, or time of day | yes | A length where the tiers go by length, a time of day where they go by time of day. A time of day may be `"24:00"`, the end of the day, and no later; the file writes it so |
| `paid_per` | length | yes | |
| `amount` | amount | yes | May be `0`. A free booking has a price of zero, entered on purpose |


## Load, validate and export

These commands use the manually managed stack in [Running](running.md#alternative-manage-the-local-stack-yourself).
With `bin/example`, the fictional setup is loaded automatically; `bin/example reset`
recreates it.

```sh
bin/dev exec app python manage.py load_setup setups/example.toml --check
bin/dev exec app python manage.py load_setup setups/example.toml
bin/dev exec app python manage.py export_setup harbourview
```

A load creates a new organization. It does not patch an existing organization.
`--check` runs the same validation and transaction, then keeps nothing. A failed
load applies no partial configuration. Change a running setup in Admin, then
export it. An export contains current configuration rather than the original file.

References use configuration names or organization-scoped numbers. Names must
resolve within the file. Asset/container/price-book numbers are preserved;
spots and groups are exported in order, compacting gaps while retaining their
relationships. Calendar start dates omitted from a file use the load date.

Exports take a coherent database capture and preserve supported text values.
Member and transaction exports are separate operations. Provider agreements,
SMTP and secrets must be configured independently on the new host. A setup file
does not transfer a domain or grant rights to the RandiRandi trademark.

## Bookings from an earlier calendar

An organization that moves from another calendar brings its coming bookings
along once, after its members are imported. Two commands do it:

```sh
bin/dev exec app python manage.py match_old_bookings harbourview titles.json
bin/dev exec app python manage.py import_old_bookings harbourview old.json --administrator sam@harbourview.example
bin/dev exec app python manage.py import_old_bookings harbourview old.json --administrator sam@harbourview.example --bring-in
```

`match_old_bookings` reads the old titles ("Kari N", "Per A.E.") and answers
which member each one names; it changes nothing. `import_old_bookings` reads
one JSON document (`"format": 1`) with three lists:

- `bookings`: a `key`, the `holder`'s email, and `pieces`, each with a
  container `configuration` number, a `category` name and `starts`/`ends` on
  the asset's clock (`2027-03-08T10:00`). `"remind": false` sends no reminder.
- `blocks`: a `key`, an `asset` number, `starts`, `ends` and a `note`, for a
  booking whose holder has no account yet: nobody books the time meanwhile.
- `events`: a `key`, `title`, `category` (`social`, `volunteer_work`,
  `organization`, `open_house`), the `contact`'s email, a `description`, an
  `asset` number or `null`, and either `starts`/`ends` or a weekly repeat
  (`weekdays` 0–6 from Monday, `first_date`, `end_date` or `null`,
  `start_time`, `end_time`).

A booking moves in settled: confirmed, nothing to pay, no payment and no
confirmation email; its lines keep the price tiers at 0, so a cancel refunds
nothing. Each piece must fit its container (weekdays, hours, step, length,
category) and free time, as a member's booking must; how far ahead, how many a
day and the categories of the holder's role do not apply, as an administrator
brings it in, and the audit log records the administrator for each order.
Bookings go in first, then blocks, then events, so a repeating event skips the
days a booking holds and names them. Without `--bring-in` the command does all
of it in a transaction it rolls back and prints what it would do. Each entry is
saved on its own; run again, an entry already in is "in already", a booking
cancelled since included. A block or event removed since comes back if the
same document is run again, and a booking whose holder has an account since
is brought in beside its block: run it once, and again only to finish refused
entries.

Times are on the asset's clock, converted to UTC as a member's booking is; a
time that summer time skips or repeats is refused. A booking or block that is
over is "past"; one under way is refused as "under way", and the rest of its
time is an administrator's to block. A refused booking leaves its time free:
put a holder without an account in `blocks`, not `bookings`. The output holds
keys, outcomes, dates and order references, never a name.

## Validation

Unknown keys and sections are refused. General checks include:

- organization-scoped references and role/category access;
- valid continuous asset availability, including periods over midnight;
- recurrence, interval and duration bounds and self-overlap;
- floor-plan/group membership and exclusive/shared consistency;
- category hours inside container hours;
- price basis, supported money bounds and complete applicable tier coverage;
- conflicting shared floor plans on the same asset;
- lossless legacy-format representation when an older format is requested.

A category without price coverage on a paid offer is not offered. Check the
calendar and price explanation as each intended role after loading. Importing
real members and testing a real payment are separate authorized activities.
