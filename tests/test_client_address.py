# SPDX-License-Identifier: AGPL-3.0-only
"""Forwarded spoof values never become a login audit/rate-limit address."""

import pytest
from django.core.exceptions import ImproperlyConfigured, SuspiciousOperation
from django.test import RequestFactory, override_settings

from randirandi import infisical
from randirandi.core.network import client_address
from randirandi.environment import client_address_source


def test_direct_requests_ignore_both_forwarded_headers() -> None:
    request = RequestFactory().get(
        "/login/",
        REMOTE_ADDR="192.0.2.3",
        HTTP_X_FORWARDED_FOR="198.51.100.4",
        HTTP_X_ENVOY_EXTERNAL_ADDRESS="198.51.100.5",
    )
    with override_settings(CLIENT_ADDRESS_SOURCE="direct"):
        assert client_address(request) == "192.0.2.3"


@pytest.mark.parametrize("peer", ["127.0.0.1", "::1"])
@pytest.mark.parametrize(
    "forged", ["198.51.100.1", "garbage", "198.51.100.1, 203.0.113.3"]
)
def test_hosted_boundary_uses_only_overwritten_external_address(
    peer: str, forged: str
) -> None:
    request = RequestFactory().get(
        "/login/",
        REMOTE_ADDR=peer,
        HTTP_X_FORWARDED_FOR=forged,
        HTTP_X_ENVOY_EXTERNAL_ADDRESS="2001:db8::1",
    )
    with override_settings(CLIENT_ADDRESS_SOURCE="scaleway"):
        assert client_address(request) == "2001:db8::1"


@pytest.mark.parametrize(
    ("peer", "header"),
    [
        ("192.0.2.3", "198.51.100.3"),
        ("bad", "198.51.100.3"),
        ("127.0.0.1", ""),
        ("127.0.0.1", "bad"),
        ("127.0.0.1", "198.51.100.3, 203.0.113.4"),
    ],
)
def test_uncertain_hosted_address_refuses_instead_of_using_shared_proxy(
    peer: str, header: str
) -> None:
    request = RequestFactory().get(
        "/login/", REMOTE_ADDR=peer, HTTP_X_ENVOY_EXTERNAL_ADDRESS=header
    )
    with (
        override_settings(CLIENT_ADDRESS_SOURCE="scaleway"),
        pytest.raises(
            SuspiciousOperation, match="client address could not be verified"
        ),
    ):
        client_address(request)


def test_source_configuration_is_explicit_and_has_no_automatic_header_detection() -> (
    None
):
    assert client_address_source({}) == "direct"
    assert client_address_source({infisical.CLIENT_ID: "fictional"}) == "direct"
    assert (
        client_address_source(
            {
                infisical.CLIENT_ID: "fictional",
                "RANDIRANDI_CLIENT_ADDRESS_SOURCE": "direct",
            }
        )
        == "direct"
    )
    with pytest.raises(ImproperlyConfigured, match="CLIENT_ADDRESS_SOURCE"):
        client_address_source({"RANDIRANDI_CLIENT_ADDRESS_SOURCE": "forwarded"})
