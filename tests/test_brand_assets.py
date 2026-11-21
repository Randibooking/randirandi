# SPDX-License-Identifier: AGPL-3.0-only
"""Every current consumer uses the approved product-owned portrait exports."""

from tools.brand_assets import ROOT, check_or_sync


def test_runtime_brand_exports_match_canonical_package() -> None:
    assert check_or_sync(ROOT) == 11
