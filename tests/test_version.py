# SPDX-License-Identifier: AGPL-3.0-only
"""Release identity is validated and points to the running tag."""

import pytest

from randirandi.version import release


def test_development_does_not_offer_a_nonexistent_public_release() -> None:
    assert release("dev", "").source_url == ""


def test_a_release_links_its_exact_tag() -> None:
    built = release("v1.2.3", "a" * 40)
    assert built.version == "v1.2.3"
    assert built.source_url.endswith("/tree/v1.2.3")


@pytest.mark.parametrize(
    ("version", "revision"),
    [
        ("main", "a" * 40),
        ("v1.2.3/other", "a" * 40),
        ("v1.2.3", "short"),
        ("dev", "a" * 40),
    ],
)
def test_an_invalid_release_cannot_claim_corresponding_source(
    version: str, revision: str
) -> None:
    with pytest.raises(ValueError):
        release(version, revision)
