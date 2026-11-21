# SPDX-License-Identifier: AGPL-3.0-only
"""The environment and the test switches (control C14)."""

import pytest
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

from randirandi import infisical
from randirandi.environment import (
    AUTHENTICATOR_KEY,
    DEFAULT_DOCUMENTATION_URL,
    DOCUMENTATION,
    GATE,
    GATE_STATE,
    SANDBOX_MAIL_TO,
    Environment,
    authenticator_key,
    documentation_url,
    load,
    sandbox_mail_to,
    which,
)

KEY_VALUE = "ab" * 32
KEY = {AUTHENTICATOR_KEY: KEY_VALUE}
PRODUCTION = {**KEY, "RANDIRANDI_ENVIRONMENT": "production", GATE_STATE: "open"}


def test_sandbox_turns_the_test_switches_on() -> None:
    deployment = load({"RANDIRANDI_ENVIRONMENT": "sandbox"})

    assert deployment.environment is Environment.SANDBOX
    assert deployment.debug is False
    assert deployment.switches.email_to_console is True
    assert deployment.switches.payment_test_environment is True
    assert deployment.switches.made_up_members is True


def test_production_turns_everything_off() -> None:
    deployment = load(PRODUCTION)

    assert deployment.environment is Environment.PRODUCTION
    assert deployment.debug is False
    assert deployment.switches.email_to_console is False
    assert deployment.switches.payment_test_environment is False
    assert deployment.switches.made_up_members is False


@pytest.mark.parametrize(
    "switch",
    [
        "RANDIRANDI_DEBUG",
        "RANDIRANDI_EMAIL_TO_CONSOLE",
        "RANDIRANDI_PAYMENT_TEST_ENVIRONMENT",
        "RANDIRANDI_MADE_UP_MEMBERS",
    ],
)
def test_production_does_not_start_with_a_switch_on(switch: str) -> None:
    with pytest.raises(ImproperlyConfigured, match=switch):
        load({**PRODUCTION, switch: "1"})


def test_production_starts_with_the_switches_written_as_off() -> None:
    deployment = load(
        {
            **PRODUCTION,
            "RANDIRANDI_DEBUG": "0",
            "RANDIRANDI_EMAIL_TO_CONSOLE": "off",
            "RANDIRANDI_PAYMENT_TEST_ENVIRONMENT": "",
        }
    )

    assert deployment.switches.email_to_console is False


def test_a_sandbox_can_send_real_email() -> None:
    deployment = load(
        {"RANDIRANDI_ENVIRONMENT": "sandbox", "RANDIRANDI_EMAIL_TO_CONSOLE": "0"}
    )

    assert deployment.switches.email_to_console is False


def test_the_sandbox_mail_list_is_read_in_lower_case_without_repeats() -> None:
    assert sandbox_mail_to({}) == ()
    assert sandbox_mail_to({SANDBOX_MAIL_TO: " , "}) == ()
    listed = {
        SANDBOX_MAIL_TO: " Owner@Example.test, second@example.test ,OWNER@example.test"
    }
    assert sandbox_mail_to(listed) == ("owner@example.test", "second@example.test")
    deployment = load({"RANDIRANDI_ENVIRONMENT": "sandbox", **listed})
    assert deployment.switches.sandbox_mail_to == sandbox_mail_to(listed)


def test_the_sandbox_mail_list_must_hold_addresses_and_does_not_repeat_them() -> None:
    with pytest.raises(ImproperlyConfigured, match=SANDBOX_MAIL_TO) as error:
        sandbox_mail_to({SANDBOX_MAIL_TO: "owner@example.test, not-an-address"})
    assert "not-an-address" not in str(error.value)


def test_the_sandbox_mail_list_needs_the_console_on() -> None:
    with pytest.raises(ImproperlyConfigured, match=SANDBOX_MAIL_TO):
        load(
            {
                "RANDIRANDI_ENVIRONMENT": "sandbox",
                "RANDIRANDI_EMAIL_TO_CONSOLE": "0",
                SANDBOX_MAIL_TO: "owner@example.test",
            }
        )


def test_production_does_not_start_with_the_sandbox_mail_list() -> None:
    with pytest.raises(ImproperlyConfigured, match=SANDBOX_MAIL_TO):
        load(
            {
                **PRODUCTION,
                SANDBOX_MAIL_TO: "owner@example.test",
            }
        )
    deployment = load({**PRODUCTION, SANDBOX_MAIL_TO: ""})
    assert deployment.switches.sandbox_mail_to == ()


@pytest.mark.parametrize(
    "switch",
    ["RANDIRANDI_PAYMENT_TEST_ENVIRONMENT", "RANDIRANDI_MADE_UP_MEMBERS"],
)
def test_a_sandbox_never_leaves_test_payment_or_made_up_members(switch: str) -> None:
    with pytest.raises(ImproperlyConfigured, match=switch):
        load({"RANDIRANDI_ENVIRONMENT": "sandbox", switch: "0"})


@pytest.mark.parametrize("value", ["", "staging", "prod", "Production"])
def test_the_environment_has_no_default_and_two_values(value: str) -> None:
    with pytest.raises(ImproperlyConfigured, match="RANDIRANDI_ENVIRONMENT"):
        load({"RANDIRANDI_ENVIRONMENT": value})


def test_a_switch_that_is_neither_on_nor_off_is_refused() -> None:
    with pytest.raises(ImproperlyConfigured, match="RANDIRANDI_DEBUG"):
        load({"RANDIRANDI_ENVIRONMENT": "sandbox", "RANDIRANDI_DEBUG": "maybe"})


def test_the_deploy_check_is_quiet_about_printed_email_only_under_the_switch() -> None:
    assert settings.SWITCHES.email_to_console is True
    assert settings.SILENCED_SYSTEM_CHECKS == ["auth.W004", "mail.E001"]
    # Email is unique within an organization. A global/password backend would
    # violate that contract; The scoped passwordless backend supplies it.
    assert settings.AUTHENTICATION_BACKENDS == [
        "randirandi.core.authentication.EmailCodeBackend"
    ]


# A machine identity is what a deployed container has and a laptop has not.
DEPLOYED = {infisical.CLIENT_ID: "made-up-client-id", **KEY}
GATE_PASSWORD = "made-up-" * 4


def test_a_deployed_sandbox_starts_behind_its_gate() -> None:
    deployment = load(
        {"RANDIRANDI_ENVIRONMENT": "sandbox", **DEPLOYED, GATE: GATE_PASSWORD}
    )

    assert deployment.switches.access_gate is True


OPEN = "RANDIRANDI_SANDBOX_OPEN"
SANDBOX = {"RANDIRANDI_ENVIRONMENT": "sandbox"}


def test_a_deployed_sandbox_starts_opened_on_purpose() -> None:
    deployment = load({**SANDBOX, **DEPLOYED, OPEN: "on"})

    assert deployment.switches.access_gate is False
    assert deployment.switches.sandbox_open is True
    assert deployment.switches.payment_test_environment is True
    assert deployment.switches.made_up_members is True
    assert deployment.switches.email_to_console is True


def test_a_deployed_sandbox_without_gate_or_opening_does_not_start() -> None:
    with pytest.raises(ImproperlyConfigured, match=GATE):
        load({**SANDBOX, **DEPLOYED})
    with pytest.raises(ImproperlyConfigured, match=OPEN):
        load({**SANDBOX, **DEPLOYED, OPEN: "off"})


def test_a_sandbox_with_the_gate_and_the_opening_is_refused() -> None:
    with pytest.raises(ImproperlyConfigured, match="both set"):
        load({**SANDBOX, **DEPLOYED, GATE: GATE_PASSWORD, OPEN: "on"})


def test_production_does_not_start_opened() -> None:
    with pytest.raises(ImproperlyConfigured, match=OPEN):
        load({**PRODUCTION, **DEPLOYED, OPEN: "on"})


def test_a_sandbox_on_the_laptop_needs_no_gate() -> None:
    deployment = load({"RANDIRANDI_ENVIRONMENT": "sandbox"})

    assert deployment.switches.access_gate is False


def test_deployed_production_can_explicitly_open() -> None:
    deployment = load({**PRODUCTION, **DEPLOYED})

    assert deployment.switches.access_gate is False


def test_the_environment_can_be_asked_for_by_itself() -> None:
    assert which(PRODUCTION) is Environment.PRODUCTION
    with pytest.raises(ImproperlyConfigured, match="RANDIRANDI_ENVIRONMENT"):
        which({})


def test_the_authenticator_key_is_required_in_production_and_a_deployed_sandbox() -> (
    None
):
    for environ in (
        {"RANDIRANDI_ENVIRONMENT": "production"},
        {"RANDIRANDI_ENVIRONMENT": "production", AUTHENTICATOR_KEY: "  "},
        {"RANDIRANDI_ENVIRONMENT": "sandbox", infisical.CLIENT_ID: "made-up"},
    ):
        with pytest.raises(ImproperlyConfigured, match=AUTHENTICATOR_KEY):
            load(environ)


@pytest.mark.parametrize(
    "value",
    ["short", "ab" * 31, "ab" * 33, "AB" * 32, "zz" * 32, " ".join(["ab"] * 32)],
)
def test_a_malformed_authenticator_key_is_refused_without_repeating_it(
    value: str,
) -> None:
    for environment in ("sandbox", "production"):
        with pytest.raises(ImproperlyConfigured, match="64") as refusal:
            load({"RANDIRANDI_ENVIRONMENT": environment, AUTHENTICATOR_KEY: value})
        assert value not in str(refusal.value)


def test_a_laptop_sandbox_may_leave_the_authenticator_key_unset() -> None:
    assert (
        load({"RANDIRANDI_ENVIRONMENT": "sandbox"}).environment is Environment.SANDBOX
    )
    assert (
        authenticator_key({"RANDIRANDI_ENVIRONMENT": "sandbox", **KEY}, required=True)
        == KEY_VALUE
    )


@pytest.mark.parametrize("state", [None, "", "unknown", "on", "off"])
def test_production_requires_an_explicit_gate_state(state: str | None) -> None:
    environ = {**KEY, "RANDIRANDI_ENVIRONMENT": "production"}
    if state is not None:
        environ[GATE_STATE] = state
    with pytest.raises(ImproperlyConfigured, match=GATE_STATE):
        load(environ)


@pytest.mark.parametrize("password", [None, "", "short", " " * 24])
def test_closed_production_requires_a_usable_gate_password(
    password: str | None,
) -> None:
    environ = {**PRODUCTION, GATE_STATE: "closed"}
    if password is not None:
        environ[GATE] = password
    with pytest.raises(ImproperlyConfigured, match=GATE) as error:
        load(environ)
    if password and password.strip():
        assert password not in str(error.value)


def test_production_gate_can_close_and_open_with_the_same_stored_password() -> None:
    environ = {**PRODUCTION, **DEPLOYED, GATE: GATE_PASSWORD}
    closed = load({**environ, GATE_STATE: "closed"})
    opened = load({**environ, GATE_STATE: "open"})
    assert closed.switches.access_gate
    assert not opened.switches.access_gate
    for deployment in (closed, opened):
        assert not deployment.debug
        assert not deployment.switches.payment_test_environment
        assert not deployment.switches.email_to_console
        assert not deployment.switches.made_up_members


@pytest.mark.parametrize("state", ["open", "closed"])
def test_sandbox_rejects_production_gate_state(state: str) -> None:
    with pytest.raises(ImproperlyConfigured, match=GATE_STATE):
        load({**SANDBOX, GATE_STATE: state})


def test_documentation_keeps_the_existing_destination_until_configured() -> None:
    assert documentation_url({}) == DEFAULT_DOCUMENTATION_URL
    assert documentation_url({DOCUMENTATION: ""}) == DEFAULT_DOCUMENTATION_URL


@pytest.mark.parametrize(
    "url",
    [
        "https://help.example.org/index-nb.html?from=app&role=admin#start",
        "http://localhost:8081/help/index-en.html",
    ],
)
def test_operator_can_select_a_help_destination(url: str) -> None:
    assert documentation_url({DOCUMENTATION: url}) == url


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "//help.example.org/",
        "/help/",
        "not-a-url",
        "https://name:password@example.org/",
        "https://name@example.org/",
        "https://example.org/\n",
        "https://example.org/\tpath",
    ],
)
def test_invalid_help_destination_is_refused_without_echoing_it(url: str) -> None:
    with pytest.raises(ImproperlyConfigured, match=DOCUMENTATION) as failure:
        documentation_url({DOCUMENTATION: url})
    assert url not in str(failure.value)
