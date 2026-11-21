# RandiRandi artwork

`logo-package-v2/` contains the canonical smooth portrait and its exports.
The app uses the pale filled badge with one outline at 64 CSS pixels beside
the product name. Email uses the matching PNG128 at 64 CSS pixels. Favicons,
home-screen icons and the README use the mapped exports in `asset-sources.json`.

Keep proportions and the single outline. Use an empty alternative text beside
the written name; a standalone linked logo needs an accessible link name.
The app theme remains dark even though the filled badge is a pale asset.

`uv run --locked python -m tools.brand_assets check` verifies source hashes
and all runtime copies; `sync` restores the copies from the package.
The Vipps mark is a separate third-party asset. See its own README and
[TRADEMARKS.md](../../TRADEMARKS.md) for the trademark boundary.

The RandiRandi portrait artwork is licensed under AGPL-3.0-only. Trademark
rights remain reserved as described in TRADEMARKS.md. Vipps is separate.
