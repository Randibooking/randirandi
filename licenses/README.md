<!-- SPDX-License-Identifier: CC-BY-SA-4.0 -->
# Third-party licences

`bin/licenses check` checks the locked Python/npm packages, base-image pins and
GitHub actions against the declared allowlist and hand-accepted metadata.
It checks free use and notice currency; it is not an image redistribution audit.

`texts/` retains third-party licence texts. Browser notices are in
`randirandi/core/static/core/third-party-notices.txt`; after browser dependencies
change, review their required notices and run `bin/licenses stamp`.

Own source/header licensing is declared in `source-licences.json` and checked
by `python -m tools.source_licenses`. Documentation is CC BY-SA 4.0; third-party
notices, fonts, MUI-derived files and the Vipps mark retain their specific terms.

Before distributing an application container, review the installed Python
packages, Debian packages and bundled native libraries, retain their notices
and meet any corresponding-source duties. Existing acceptance reasons that say
“hosting only” establish no redistribution permission by themselves. Container
publication remains an explicit release gate until that audit is complete.
