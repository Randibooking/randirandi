<!-- SPDX-License-Identifier: CC-BY-SA-4.0 -->
# Data model

An organization owns its configuration and member/transaction graph. Rows are
scoped to that organization; a relationship crossing organization boundaries
is refused. Configuration names and values are not universal booking rules.
See [Booking model](booking-model.md) for those rules.

## Configuration

| Area | Main objects and links |
| --- | --- |
| Organization | Subdomain, contact email, language, time zone, currency, limits and retention |
| Access | Roles permit booking categories and independently grant Admin access |
| Physical space | Spaces contain assets; an optional marked asset represents the whole space |
| Inventory | Asset → floor plans → spots; a group contains spots from one floor plan |
| Availability | Dated calendars and weekly spans; dated closures |
| Offers | Booking configurations link asset, booking type, floor plan, categories and time rules |
| Pricing | Price books → entries by configuration/category/group → ordered tiers |

A spot's identity and floor-plan relationships cannot change after transactions
rely on them. Structural edits have model and database checks. A foreign key
alone is not the whole immutability contract.

`export_setup` captures configuration from one coherent database state. It
locks configuration tables before reading, reloads the organization and
releases command-owned locks when it returns. A caller's enclosing transaction
can extend lock lifetime. Keep such transactions short. Member lists, orders,
payments, credentials and audit records are not setup configuration.

## Members and access

A member has a role, contact identity and access state. Memberships represent
units or other membership relationships; a person can have retained links
from more than one import. Imports record their parser ID, file metadata,
outcome, counts and problems. Parser input is not accepted as trusted data.

A full source import reconciles identity claims before changing groups.
Contradictory email/phone claims are refused. A large proposed revocation is
stopped for investigation. Manual member corrections, blocking and imported
status are distinct operations with audit history.

Email/phone sign-in uses one-time email codes. Administrator sessions additionally
require an authenticator. Authenticator secrets are encrypted with an
operator-supplied key. Challenge, pending-MFA and authenticated-session deadlines
are independent; delayed mail does not extend authentication rights indefinitely.

## Transactions and money

Orders group booking lines and their inventory occupation. Payment attempts,
provider events and refunds link to the retained transaction graph. Amounts
use validated monetary representations, not floating-point approximations.

Checkout, identity reconciliation, cancellation and refund operations use
transactions and locks. External provider calls and callbacks may fail or be
retried. Queued work preserves the pending state until its required effect or
record succeeds. A captured payment and a fulfilled booking are separate facts.

## Audit, export and retention

Audit records identify the action, actor and relevant subjects/targets. Member
export follows both actor and subject links through the member's retained
transaction graph, returning explicit safe fields. Related records can expose
personal information even when the member row is not their direct parent.

Retention is configurable. Removing current membership links alone is not
sufficient erasure; historical imported links and related audit/transaction
records also matter. Monetary totals needed for later reconciliation survive
mixed-age retention without retaining unnecessary old booking detail.

The application database login has a declared privilege contract in
`randirandi/ops/privileges.py`. Migration/restore operations apply that contract;
`check_privileges` compares effective grants. Audit-history protection includes
row updates/deletes, TRUNCATE, ownership and authority reachable through SET ROLE.
An operator login that owns the schema is separate from the application login.

## Implementation map

- `randirandi/core/models/`: organization, configuration, member and transaction models.
- `randirandi/core/validator.py`: cross-record booking configuration validation.
- `randirandi/core/member_import.py`: identity reconciliation and import outcomes.
- `randirandi/core/member_data.py`: personal-data export.
- `randirandi/setup_file.py`: portable setup reader/writer.
- `randirandi/ops/`: job loop, health, backup and privilege operations.
- `tests/`: model, migration, concurrency, permissions and HTTP regression checks.

Model documentation describes implemented behavior. A plausible scenario or a
model-valid row does not by itself prove that every corresponding UI action exists.
