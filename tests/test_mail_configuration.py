# SPDX-License-Identifier: AGPL-3.0-only
"""SMTP settings and the actual framework TLS/authentication call sequence."""

import io
import ssl
from unittest import mock

import pytest
from django.core import mail
from django.core.exceptions import ImproperlyConfigured
from django.test import override_settings

from randirandi.mail_configuration import CONSOLE, mailers

CREDENTIALS = {
    "RANDIRANDI_SMTP_USER": "fictional-project",
    "RANDIRANDI_SMTP_PASSWORD": "fictional-test-only",
}


@pytest.mark.parametrize("security", ["starttls", "tls"])
def test_actual_backend_uses_verified_tls_before_authentication(security: str) -> None:
    configuration = mailers(
        {**CREDENTIALS, "RANDIRANDI_SMTP_SECURITY": security}, console=False
    )
    with (
        override_settings(MAILERS=configuration),
        mock.patch("smtplib.SMTP") as plain,
        mock.patch("smtplib.SMTP_SSL") as implicit,
    ):
        connection = (implicit if security == "tls" else plain).return_value
        connection.sendmail.return_value = {}
        message = mail.EmailMessage(
            "Code", "Your fictional code", "post@example.test", ["member@example.test"]
        )
        assert message.send(using="default") == 1
        factory = implicit if security == "tls" else plain
        args, kwargs = factory.call_args
        assert args == ("smtp.tem.scaleway.com", 465 if security == "tls" else 587)
        assert kwargs["timeout"] == 10
        if security == "starttls":
            context = connection.starttls.call_args.kwargs["context"]
            assert [call[0] for call in connection.mock_calls][:2] == [
                "starttls",
                "login",
            ]
            implicit.assert_not_called()
        else:
            context = kwargs["context"]
            connection.starttls.assert_not_called()
            plain.assert_not_called()
        assert context.check_hostname is True
        assert context.verify_mode == ssl.CERT_REQUIRED
        connection.login.assert_called_once_with(
            "fictional-project", "fictional-test-only"
        )


@pytest.mark.parametrize("missing", list(CREDENTIALS))
def test_missing_credentials_stop_non_console_startup(missing: str) -> None:
    with pytest.raises(ImproperlyConfigured, match=missing) as error:
        mailers({k: v for k, v in CREDENTIALS.items() if k != missing}, console=False)
    assert "fictional-test-only" not in str(error.value)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("RANDIRANDI_SMTP_HOST", "https://smtp.example.test"),
        ("RANDIRANDI_SMTP_HOST", "smtp.example.test\r\n"),
        ("RANDIRANDI_SMTP_SECURITY", "none"),
        ("RANDIRANDI_SMTP_SECURITY", ""),
        ("RANDIRANDI_SMTP_PORT", "0"),
        ("RANDIRANDI_SMTP_PORT", "65536"),
        ("RANDIRANDI_SMTP_PORT", "587.0"),
    ],
)
def test_invalid_transport_settings_fail_without_disclosing_values(
    name: str, value: str
) -> None:
    with pytest.raises(ImproperlyConfigured, match=name):
        mailers({**CREDENTIALS, name: value}, console=False)


def test_console_and_alternate_provider_configuration() -> None:
    assert mailers({}, console=True)["default"]["BACKEND"].endswith(
        "console.EmailBackend"
    )
    alternate = mailers(
        {
            **CREDENTIALS,
            "RANDIRANDI_SMTP_HOST": "smtp.example.test",
            "RANDIRANDI_SMTP_PORT": "2465",
            "RANDIRANDI_SMTP_SECURITY": "tls",
        },
        console=False,
    )
    assert alternate["default"]["OPTIONS"]["host"] == "smtp.example.test"
    assert alternate["default"]["OPTIONS"]["port"] == 2465


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("delivery", ["login", "reset"])
def test_refused_tls_closes_real_socket_without_sending_credentials(
    delivery: str,
) -> None:
    import smtplib
    import socket
    from threading import Thread

    from randirandi.core.login_delivery import enqueue_code, send_pending_codes
    from randirandi.core.second_factor import reset_authenticator
    from randirandi.core.security_notices import send_security_notices
    from tests import made_up

    made = made_up.everything()
    if delivery == "login":
        enqueue_code(
            organization=made.organization,
            identifier=made.member.email,
            browser="fictional-browser-binding-for-smtp-cleanup",
            network="192.0.2.12",
        )
        send = send_pending_codes
    else:
        reset_authenticator(
            organization=made.organization,
            member_id=made.administrator.pk,
            no_other_administrator_available=True,
        )
        send = send_security_notices
    commands: list[bytes] = []
    clients: list[smtplib.SMTP] = []
    original = smtplib.SMTP

    def connect(*args: object, **kwargs: object) -> smtplib.SMTP:
        # The actual constructor is exercised; retain the object to inspect its socket.
        client = original(*args, **kwargs)  # type: ignore[arg-type]
        clients.append(client)
        return client

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(5)

        def server() -> None:
            connection, _address = listener.accept()
            with connection, connection.makefile("rwb") as stream:
                connection.settimeout(5)
                stream.write(b"220 fictional SMTP\r\n")
                stream.flush()
                while line := stream.readline():
                    command = line.split()[0].upper()
                    commands.append(command)
                    stream.write(
                        b"221 bye\r\n" if command == b"QUIT" else b"250 fictional\r\n"
                    )
                    stream.flush()
                    if command == b"QUIT":
                        break

        thread = Thread(target=server, daemon=True)
        thread.start()
        configuration = mailers(
            {
                **CREDENTIALS,
                "RANDIRANDI_SMTP_HOST": "127.0.0.1",
                "RANDIRANDI_SMTP_PORT": str(listener.getsockname()[1]),
            },
            console=False,
        )
        try:
            with (
                override_settings(MAILERS=configuration),
                mock.patch("smtplib.SMTP", side_effect=connect),
            ):
                send()
            assert len(clients) == 1
            assert clients[0].sock is None
            assert commands == [b"EHLO", b"QUIT"]
        finally:
            thread.join(6)
            assert not thread.is_alive()


OWNER = "owner@example.test"


def routed(*recipients: str, cc: tuple[str, ...] = ()) -> mail.EmailMessage:
    return mail.EmailMessage(
        "Code", "Your fictional code", "post@example.test", list(recipients), cc=cc
    )


def send_all(*messages: mail.EmailMessage) -> tuple[int, list[str], list[str]]:
    """What was sent, the addresses that went by SMTP and the ones printed."""
    configuration = mailers(
        CREDENTIALS, console=True, sandbox_to=[OWNER, "second@example.test"]
    )
    with (
        override_settings(MAILERS=configuration),
        mock.patch("smtplib.SMTP") as plain,
        mock.patch("sys.stdout", new_callable=io.StringIO) as printed,
    ):
        plain.return_value.sendmail.return_value = {}
        with mail.mailers["default"] as connection:
            sent = connection.send_messages(list(messages))
        by_smtp = [
            call.args[1][0] for call in plain.return_value.sendmail.call_args_list
        ]
    return (
        sent,
        by_smtp,
        [
            line.removeprefix("To: ")
            for line in printed.getvalue().splitlines()
            if line.startswith("To: ")
        ],
    )


def test_without_a_list_the_console_is_the_only_mailer() -> None:
    assert mailers({}, console=True) == {"default": CONSOLE}
    assert mailers({}, console=True, sandbox_to=[]) == {"default": CONSOLE}


def test_a_list_adds_the_router_and_needs_smtp_settings() -> None:
    configuration = mailers(CREDENTIALS, console=True, sandbox_to=[OWNER])
    assert configuration["default"]["BACKEND"].endswith("SandboxRouter")
    assert configuration["console"] == CONSOLE
    assert configuration["smtp"]["OPTIONS"]["host"] == "smtp.tem.scaleway.com"
    for missing in CREDENTIALS:
        with pytest.raises(ImproperlyConfigured, match=missing):
            mailers(
                {k: v for k, v in CREDENTIALS.items() if k != missing},
                console=True,
                sandbox_to=[OWNER],
            )


def test_every_recipient_on_the_list_goes_by_smtp() -> None:
    sent, by_smtp, printed = send_all(routed(OWNER, "second@example.test"))
    assert (sent, by_smtp, printed) == (1, [OWNER], [])


@pytest.mark.django_db
def test_one_recipient_off_the_list_prints_the_whole_message() -> None:
    sent, by_smtp, printed = send_all(
        routed(OWNER, "member@example.test"), routed(OWNER, cc=("member@example.test",))
    )
    assert (sent, by_smtp) == (2, [])
    assert len(printed) == 2


@pytest.mark.django_db
def test_no_recipient_on_the_list_prints() -> None:
    sent, by_smtp, printed = send_all(routed("member@example.test"))
    assert (sent, by_smtp, printed) == (1, [], ["member@example.test"])


def test_a_message_without_recipients_prints() -> None:
    sent, by_smtp, _ = send_all(routed())
    assert (sent, by_smtp) == (1, [])


def test_recipients_are_compared_in_lower_case() -> None:
    sent, by_smtp, printed = send_all(routed("Owner@Example.TEST"))
    assert (sent, by_smtp, printed) == (1, ["Owner@Example.TEST"], [])


@pytest.mark.django_db
def test_a_mixed_batch_is_sorted_message_by_message() -> None:
    sent, by_smtp, printed = send_all(
        routed(OWNER), routed("member@example.test"), routed("second@example.test")
    )
    assert sent == 3
    assert by_smtp == [OWNER, "second@example.test"]
    assert printed == ["member@example.test"]
