# SPDX-License-Identifier: AGPL-3.0-only
"""The audit log's JSON answer : the gate, the filters, the sort,
the pages, the search, the names looked up and the time zone."""

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from django.contrib.sessions.backends.db import SessionStore
from django.core.exceptions import PermissionDenied
from django.test import Client, RequestFactory

from randirandi.core import admin_views, audit_log
from randirandi.core.audit_types import OBJECT_KINDS, AuditAction
from randirandi.core.models import (
    AuditEvent,
    Authenticator,
    Member,
    MemberImport,
    Order,
    Organization,
    Payment,
    Refund,
    Role,
)
from tests import made_up
from tests.capacity_app import application_login
from tests.test_admin_shell import sign_in_administrator
from tests.test_login_sessions import (  # noqa: F401
    HOST,
    get,
    page_settings,
    post,
    request_code,
)
from tests.test_second_factor import clocks, token  # noqa: F401

pytestmark = pytest.mark.django_db(transaction=True)
PATH = "/admin/audit/events/"
FIRST = datetime(2026, 3, 1, 8, 0, tzinfo=UTC)


@pytest.fixture
def made() -> made_up.MadeUp:
    return made_up.everything()


def event(
    made: made_up.MadeUp,
    *,
    by: Member | None = None,
    action: AuditAction = AuditAction.INSPECT,
    kind: str = "member",
    pk: int | None = None,
    at: datetime = FIRST,
) -> AuditEvent:
    by = by or administrator(made, "Jo Bright")
    return AuditEvent.objects.create(
        organization=by.organization,
        administrator=by,
        action=action,
        object_kind=kind,
        object_id=pk or made.member.pk,
        at=at,
    )


def ask(made: made_up.MadeUp, **query: Any) -> dict[str, Any]:
    """The answer for the made-up administrator. The made-up organization has
    one event of its own; unless a test names `by`, only Jo Bright's show.
    """
    query = {"by": administrator(made, "Jo Bright").pk, **query}
    request = RequestFactory().get(PATH, query, HTTP_HOST=HOST, secure=True)
    request.session = SessionStore()
    request.session["admin_confirmed"] = True
    request.user = made.administrator
    response = admin_views.audit_events(request)
    assert response.status_code == 200
    assert "no-store" in response.headers["Cache-Control"]
    answer = json.loads(response.content)
    assert isinstance(answer, dict)
    return answer


def times(answer: dict[str, Any]) -> list[str]:
    return [row["at"] for row in answer["events"]]


def refused(made: made_up.MadeUp, **query: Any) -> int:
    request = RequestFactory().get(PATH, query, HTTP_HOST=HOST, secure=True)
    request.session = SessionStore()
    request.session["admin_confirmed"] = True
    request.user = made.administrator
    return admin_views.audit_events(request).status_code


def administrator(made: made_up.MadeUp, name: str) -> Member:
    """Another administrator of the made-up organization, made once."""
    first, last = name.split()
    found = Member.objects.of(made.organization).filter(full_name=name).first()
    return found or Member.objects.create(
        organization=made.organization,
        role=made.administrator.role,
        full_name=name,
        first_name=first,
        last_name=last,
        email=f"{first.lower()}@harbourview.example",
        phone=f"+47900{sum(map(ord, name)) % 100000:05d}",
        status=made.administrator.status,
        origin=made.administrator.origin,
    )


def test_the_gate_is_the_shells(made: made_up.MadeUp) -> None:
    signed_out = get(Client(), PATH)
    assert signed_out.status_code == 302
    assert signed_out.headers["Location"].startswith("/login/")
    member, admin = Client(), Client()
    with application_login():
        assert post(member, "/login/code/", code=request_code(member, made.member))
        assert get(member, PATH).status_code == 403
        sign_in_administrator(admin, made.administrator)
        assert get(admin, PATH).status_code == 200
        assert post(admin, PATH).status_code == 405
        head = admin.head(PATH, HTTP_HOST=HOST, secure=True)
        assert head.status_code == 405


def test_without_the_second_step_or_from_another_organization(
    made: made_up.MadeUp,
) -> None:
    request = RequestFactory().get(PATH, HTTP_HOST=HOST, secure=True)
    request.session = SessionStore()
    request.user = made.administrator
    with pytest.raises(PermissionDenied):
        admin_views.audit_events(request)
    elsewhere = made_up.everything("elsewhere")
    event(elsewhere)
    request.session["admin_confirmed"] = True
    request.user = elsewhere.administrator
    with pytest.raises(PermissionDenied):
        admin_views.audit_events(request)
    event(made)
    assert ask(made)["count"] == 1


def test_newest_first_and_the_when_column_sorts_both_ways(
    made: made_up.MadeUp,
) -> None:
    for hours in (2, 0, 1):
        event(made, at=FIRST + timedelta(hours=hours))
    assert times(ask(made)) == [
        "2026-03-01, 11:00:00", "2026-03-01, 10:00:00", "2026-03-01, 09:00:00",
    ]  # fmt: skip
    assert times(ask(made, order="oldest")) == times(ask(made))[::-1]
    assert times(ask(made, order="newest")) == times(ask(made))
    assert refused(made, order="sideways") == 400


def test_pages_of_25_and_the_last_page(made: made_up.MadeUp) -> None:
    for minute in range(60):
        event(made, at=FIRST + timedelta(minutes=minute))
    first = ask(made)
    assert (first["count"], first["pages"], first["per_page"]) == (60, 3, 25)
    assert len(first["events"]) == 25 and first["page"] == 1
    assert times(first)[0] == "2026-03-01, 09:59:00"
    last = ask(made, page=3)
    assert len(last["events"]) == 10 and times(last)[-1] == "2026-03-01, 09:00:00"
    both = times(first) + times(ask(made, page=2)) + times(last)
    assert len(set(both)) == 60
    assert ask(made, page=4)["events"] == []
    oldest = ask(made, page=3, order="oldest")
    assert times(oldest)[-1] == "2026-03-01, 09:59:00"
    for page in ("0", "-1", "x", "1.5"):
        assert refused(made, page=page) == 400


def test_nothing_matches(made: made_up.MadeUp) -> None:
    event(made)
    none = ask(made, q="zzz")
    assert (none["count"], none["pages"], none["events"]) == (0, 0, [])


def test_when_filter_is_in_the_organizations_time_zone(made: made_up.MadeUp) -> None:
    # Oslo is UTC+1 in January: 23:30 UTC on the 15th is 00:30 on the 16th.
    event(made, at=datetime(2026, 1, 15, 23, 30, tzinfo=UTC))
    event(made, at=datetime(2026, 1, 15, 22, 59, 59, tzinfo=UTC))
    event(made, at=datetime(2026, 1, 16, 22, 59, 59, tzinfo=UTC))
    event(made, at=datetime(2026, 1, 16, 23, 0, tzinfo=UTC))
    day = ask(made, **{"from": "2026-01-16", "to": "2026-01-16"})
    assert times(day) == ["2026-01-16, 23:59:59", "2026-01-16, 00:30:00"]
    period = ask(made, **{"from": "2026-01-15", "to": "2026-01-16"})
    assert period["count"] == 3
    assert ask(made, **{"from": "2026-01-17"})["count"] == 1
    assert ask(made, to="2026-01-15")["count"] == 1
    assert ask(made, **{"from": "2026-01-17", "to": "2026-01-16"})["count"] == 0
    for bad in ("2026-1-1", "2026-13-01", "today", ""):
        assert refused(made, **{"from": bad}) == 400


def test_the_stamp_follows_summer_time(made: made_up.MadeUp) -> None:
    event(made, at=datetime(2026, 7, 1, 12, 0, 5, tzinfo=UTC))
    assert times(ask(made)) == ["2026-07-01, 14:00:05"]
    Organization.objects.filter(pk=made.organization.pk).update(
        time_zone="America/New_York"
    )
    assert times(ask(made)) == ["2026-07-01, 08:00:05"]


def test_administrator_and_action_filters(made: made_up.MadeUp) -> None:
    jo, kim = administrator(made, "Jo Bright"), administrator(made, "Kim Stone")
    event(made, by=jo, action=AuditAction.INSPECT)
    event(made, by=kim, action=AuditAction.INSPECT)
    event(made, by=kim, action=AuditAction.ADMIN_LOGIN)
    assert ask(made, by=jo.pk)["count"] == 1
    assert ask(made, by=kim.pk)["count"] == 2
    assert ask(made, by=made.member.pk)["count"] == 0
    both = {"action": AuditAction.INSPECT.value}
    assert ask(made, by=kim.pk, **both)["count"] == 1
    assert ask(made, by=kim.pk, action=AuditAction.ADMIN_LOGIN.value)["count"] == 1
    assert ask(made, by=jo.pk, action=AuditAction.ADMIN_LOGIN.value)["count"] == 0
    assert refused(made, action="made it up") == 400
    assert refused(made, by="Jo") == 400


def test_everyone_is_the_whole_log(made: made_up.MadeUp) -> None:
    event(made)
    event(made, by=administrator(made, "Kim Stone"))
    request = RequestFactory().get(PATH, HTTP_HOST=HOST, secure=True)
    request.session = SessionStore()
    request.session["admin_confirmed"] = True
    request.user = made.administrator
    # The made-up organization's own event, Jo's and Kim's.
    assert json.loads(admin_views.audit_events(request).content)["count"] == 3


def test_the_filters_list_whoever_is_in_the_log_and_the_fixed_actions(
    made: made_up.MadeUp,
) -> None:
    event(made)
    answer = ask(made)
    assert [row["name"] for row in answer["administrators"]] == [
        "Jo Bright", "Sam Field",
    ]  # fmt: skip
    assert len(answer["actions"]) == len(AuditAction)
    labels = {row["value"]: row["label"] for row in answer["actions"]}
    assert labels["inspected"] == "Looked up"
    assert labels["administrator signed in"] == "Signed in"
    assert labels["exported member"] == "Exported member"
    assert labels["operator named first administrator"] == (
        "Operator named first administrator"
    )
    assert [row["label"] for row in answer["actions"]] == sorted(labels.values())
    assert (
        ask(made, action="exported member")["administrators"]
        == (answer["administrators"])
    )


def test_an_event_names_its_administrator_and_its_action(
    made: made_up.MadeUp,
) -> None:
    event(made, action=AuditAction.ADMIN_LOGIN)
    (row,) = ask(made)["events"]
    assert row["administrator"]["name"] == "Jo Bright"
    assert (row["action"], row["action_label"]) == (
        "administrator signed in", "Signed in",
    )  # fmt: skip


def expected_names(made: made_up.MadeUp) -> dict[str, tuple[str, str, int]]:
    rows = made.rows
    order = rows[Order]
    assert isinstance(order, Order)
    payment = rows[Payment]
    assert isinstance(payment, Payment)
    refund = rows[Refund]
    assert isinstance(refund, Refund)
    role = rows[Role]
    assert isinstance(role, Role)
    device = rows[Authenticator]
    assert isinstance(device, Authenticator)
    run = rows[MemberImport]
    return {
        "organization": ("Organization", made.organization.name, made.organization.pk),
        "member": ("Member", "Alex Rivers", made.member.pk),
        "asset": ("Asset", made.asset.name, made.asset.pk),
        "booking_configuration": (
            "Container", f"{made.configuration.number:03d}", made.configuration.pk,
        ),
        "role": ("Role", role.name, role.pk),
        "order": ("Reference", order.reference, order.pk),
        "booking": ("Reference", made.booking.order.reference, made.booking.pk),
        "payment": ("Payment", f"Reference {payment.order.reference}", payment.pk),
        "refund": ("Refund", f"Reference {payment.order.reference}", refund.pk),
        "authenticator": ("Authenticator", device.member.full_name, device.pk),
        "member_import": ("Member import", str(run.pk), run.pk),
    }  # fmt: skip


def test_each_kind_is_named_from_the_current_record(made: made_up.MadeUp) -> None:
    expected = expected_names(made)
    for kind, (_, _, pk) in expected.items():
        event(made, kind=kind, pk=pk)
    shown = {
        row["what"]["kind"] + "|" + row["what"]["name"] for row in ask(made)["events"]
    }
    assert shown == {f"{kind}|{name}" for kind, name, _ in expected.values()}


def test_every_named_kind_has_a_record_to_name(made: made_up.MadeUp) -> None:
    made_up.with_event(made)
    for kind, spec in audit_log.KINDS.items():
        assert kind in OBJECT_KINDS
        pk = (
            made.organization.pk
            if spec.model is Organization
            else made.rows[spec.model].pk  # type: ignore[index]
        )
        event(made, kind=kind, pk=pk)
    answer = ask(made, order="oldest")
    assert len(answer["events"]) == len(audit_log.KINDS)
    for row in answer["events"]:
        assert row["what"]["name"]


def test_a_name_is_read_when_shown(made: made_up.MadeUp) -> None:
    event(made, kind="member", pk=made.member.pk)
    assert ask(made)["events"][0]["what"]["name"] == "Alex Rivers"
    Member.objects.of(made.organization).filter(pk=made.member.pk).update(
        full_name="Alexandra Rivers"
    )
    assert ask(made)["events"][0]["what"]["name"] == "Alexandra Rivers"


def test_a_name_from_first_and_last_when_there_is_no_full_name(
    made: made_up.MadeUp,
) -> None:
    Member.objects.of(made.organization).filter(pk=made.member.pk).update(full_name="")
    event(made)
    assert ask(made)["events"][0]["what"]["name"] == "Alex Rivers"


def test_an_erased_member_shows_kind_and_number_only(made: made_up.MadeUp) -> None:
    erased = administrator(made, "Kim Stone")
    event(made, by=erased, kind="member", pk=erased.pk)
    Member.objects.of(made.organization).filter(pk=erased.pk).update(
        full_name="", first_name="", last_name=""
    )
    (row,) = ask(made, by=erased.pk)["events"]
    assert row["what"] == {
        "kind": "Member", "name": str(erased.pk), "booking": None,
        "target": {"kind": "member", "id": erased.pk},
    }  # fmt: skip
    assert row["administrator"]["name"] == f"Member {erased.pk}"
    listed = ask(made)["administrators"]
    assert {"id": erased.pk, "name": f"Member {erased.pk}"} in listed


def test_an_event_sends_the_kind_and_id_its_page_opens_by(
    made: made_up.MadeUp,
) -> None:
    expected = expected_names(made)
    run = made.rows[MemberImport]
    device = made.rows[Authenticator]
    assert isinstance(device, Authenticator)
    for kind in (
        "member",
        "authenticator",
        "member_import",
        "booking_configuration",
        "role",
        "asset",
        "block",
    ):
        event(made, kind=kind, pk=expected[kind][2] if kind in expected else 4711)
    found = {r["what"]["kind"]: r["what"]["target"] for r in ask(made)["events"]}
    assert found["Member"] == {"kind": "member", "id": made.member.pk}
    # An authenticator opens its member's page.
    assert found["Authenticator"] == {"kind": "member", "id": device.member_id}
    # A run is opened by its number among the organization's runs, oldest first.
    runs = MemberImport.objects.of(made.organization).filter(pk__lt=run.pk)
    assert found["Member import"] == {"kind": "member_import", "id": runs.count() + 1}
    assert found["Container"] == {
        "kind": "booking_configuration", "id": made.configuration.pk,
    }  # fmt: skip
    assert found["Role"]["kind"] == "role"
    assert found["Asset"] == {"kind": "asset", "id": made.asset.pk}
    assert found["Block"] == {"kind": "block", "id": 4711}


def test_an_event_has_no_target_when_nothing_opens_for_it(
    made: made_up.MadeUp,
) -> None:
    event(made, kind="member", pk=987654)  # gone
    event(made, kind="login_code", pk=31)  # a kind with no page
    event(made, kind="order", pk=made.order.pk)  # a booking: `booking`, not a target
    rows = {r["what"]["kind"]: r["what"] for r in ask(made)["events"]}
    assert rows["Member"]["target"] is None
    assert rows["Login code"]["target"] is None
    assert rows["Reference"]["target"] is None
    assert rows["Reference"]["booking"] == made.order.number


def test_a_target_that_is_gone_shows_kind_and_number_only(
    made: made_up.MadeUp,
) -> None:
    event(made, kind="member", pk=987654)
    event(made, kind="login_code", pk=31)
    rows = {r["what"]["kind"]: r["what"]["name"] for r in ask(made)["events"]}
    assert rows == {"Member": "987654", "Login code": "31"}


def test_search_finds_names_numbers_and_kinds(made: made_up.MadeUp) -> None:
    payment = made.rows[Payment]
    assert isinstance(payment, Payment)
    event(made, kind="member", pk=made.member.pk)
    event(made, kind="order", pk=payment.order.pk)
    event(made, kind="booking_configuration", pk=made.configuration.pk)
    event(made, kind="asset", pk=made.asset.pk)
    event(made, kind="organization", pk=made.organization.pk)
    event(made, kind="member", pk=987654)

    def found(q: str) -> list[str]:
        return sorted(r["what"]["kind"] for r in ask(made, q=q)["events"])

    assert found("alex riv") == ["Member"]
    assert found("ALEX") == ["Member"]
    assert found(f"Reference {payment.order.reference}") == ["Reference"]
    # A reference is found in any case, with or without the kind's word.
    assert found(payment.order.reference.lower()) == ["Reference"]
    assert found(f"reference {payment.order.reference.lower()[:4]}") == ["Reference"]
    assert found(f"{made.configuration.number:03d}") == ["Container"]
    assert found(f"Container {made.configuration.number:03d}") == ["Container"]
    assert found("container") == ["Container"]
    assert found(made.asset.name[:4]) == ["Asset"]
    assert found("member") == ["Member", "Member"]
    assert found("zzz") == []
    assert found("%") == [] and found("_") == []
    assert ask(made, q="  ALEX  ")["count"] == 1
    assert ask(made, q="alex", by=made.member.pk)["count"] == 0


def test_search_finds_a_gone_target_by_its_kind_and_number(
    made: made_up.MadeUp,
) -> None:
    event(made, kind="login_code", pk=31)
    event(made, kind="login_code", pk=32)
    assert ask(made, q="Login code 31")["count"] == 1
    assert ask(made, q="31")["count"] == 1
    assert ask(made, q="login")["count"] == 2


def test_search_is_bounded_and_its_filters_combine(made: made_up.MadeUp) -> None:
    jo, kim = administrator(made, "Jo Bright"), administrator(made, "Kim Stone")
    event(made, by=kim, action=AuditAction.EXPORT_MEMBER)
    event(made, by=jo, action=AuditAction.EXPORT_MEMBER)
    event(made, by=kim, action=AuditAction.INSPECT)
    assert ask(made, q="alex", by=kim.pk, action="exported member")["count"] == 1
    assert refused(made, q="x" * 101) == 400
    assert ask(made, q="x" * 100)["count"] == 0


def test_reading_writes_no_event(made: made_up.MadeUp) -> None:
    event(made)
    before = AuditEvent.objects.of(made.organization).count()
    client = Client()
    with application_login():
        sign_in_administrator(client, made.administrator)
        after_sign_in = AuditEvent.objects.of(made.organization).count()
        assert get(client, "/admin/audit/").status_code == 200
        assert get(client, PATH).status_code == 200
        assert get(client, PATH + "?q=alex&page=2").status_code == 200
    assert AuditEvent.objects.of(made.organization).count() == after_sign_in
    assert after_sign_in >= before


def test_the_answer_is_bounded_by_one_page(
    made: made_up.MadeUp, django_assert_max_num_queries: Any
) -> None:
    for minute in range(60):
        event(made, at=FIRST + timedelta(minutes=minute))
    # The gate, the count, the page, the names and the two lists: a few
    # queries, none per event.
    with django_assert_max_num_queries(10):
        assert len(ask(made, page=2)["events"]) == 25


def test_malformed_input_is_a_400_never_a_500(made: made_up.MadeUp) -> None:
    event(made)
    for bad in (
        {"to": "9999-12-31"},
        {"from": "9999-12-31"},
        {"from": "0001-01-01"},
        {"to": "0001-01-01"},
        {"from": "1999-12-31"},
        {"to": "2101-01-01"},
        {"by": "abc"},
        {"by": "-3"},
        {"by": "0"},
        {"page": "-1"},
        {"page": "0"},
        {"page": "99999999999999999999"},
        {"action": "nothing"},
        {"order": "up"},
    ):
        assert refused(made, **bad) == 400, bad
    assert ask(made, **{"from": "2000-01-01", "to": "2100-12-31"})["count"] == 1
    assert ask(made, page=999_999_999)["events"] == []


def test_a_kinds_word_is_matched_from_three_characters_at_a_word_start(
    made: made_up.MadeUp,
) -> None:
    # Targets that are gone, so only the kind's word can match.
    for kind in ("asset", "space", "booking_category", "booking_type"):
        event(made, kind=kind, pk=987654)
    event(made, kind="login_code", pk=31)

    def found(q: str) -> list[str]:
        return sorted(r["what"]["kind"] for r in ask(made, q=q)["events"])

    assert found("a") == [] and found("as") == []
    assert found("ass") == ["Asset"]
    assert found("ASSET") == ["Asset"]
    assert found("spa") == ["Space"]
    assert found("typ") == ["Booking type"]
    assert found("book") == ["Booking type"]
    assert found("ode") == []
    assert found("lo") == []
    assert found("log") == ["Login code"]
    assert found("code") == ["Login code"]
