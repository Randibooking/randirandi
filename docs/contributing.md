<!-- SPDX-License-Identifier: CC-BY-SA-4.0 -->
# Development

Read [CONTRIBUTING.md](../CONTRIBUTING.md) for the current contribution policy.

- `randirandi/core/` contains models, domain services and HTTP boundaries.
- `randirandi/locale/` contains complete language catalogues.
- `frontend/src/book/` and `frontend/src/admin/` contain the React/MUI pages.
- `frontend/src/theme/` supplies the fixed dark design system.
- `setups/` contains fictional configuration and acceptance scenarios.
- `tests/` contains backend, database, migration and HTTP checks.
- `bin/` exposes the supported build/check/local-running commands.

Use `uv sync --locked` for Python dependencies and the frontend lock for Node.
`bin/setup` also installs the pre-commit hook. The hook selects affected backend
tests; the full `bin/check` remains the release gate. Check time is measured;
adding a gate includes considering its cost.

Test outcomes, not an implementation's own assumptions. Identity changes need
contradictory/reordered input; setup portability needs edited round-trips;
transaction rules need coordinated writes; permission rules need the effective
application login. HTTP authentication tests include expired sessions and
refusal paths. Payment fixtures preserve provider representations and bounds.

Keep migrations reversible when that is lossless; refuse a downgrade that would
silently discard supported data. Restore the migration graph's current leaves
in migration tests. Parallel tests installing the application role use separate
PostgreSQL clusters, because roles are cluster-wide.

Do not add real databases, member lists, secrets or production captures. Update
the model/setup documentation when a general capability changes. Customer
configuration and operator deployment records are kept separately.
