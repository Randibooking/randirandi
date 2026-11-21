# SPDX-License-Identifier: AGPL-3.0-only
"""Fixed audit vocabulary: never names, contact details or free text."""

from django.db import models


class AuditAction(models.TextChoices):
    BOOTSTRAP = "operator named first administrator"
    ADD_MEMBER = "added member"
    EXPORT_MEMBER = "exported member"
    CORRECT_MEMBER = "corrected member"
    CHANGE_MEMBER_EMAIL = "changed member email"
    GAVE_ADMIN_ACCESS = "gave admin access"
    ERASE_MEMBER = "erased member"
    OPERATOR_ERASE_MEMBER = "operator erased member"
    BLOCK_MEMBER = "blocked member"
    UNBLOCK_MEMBER = "unblocked member"
    IMPORT_MEMBERS = "requested member import"
    INSPECT = "inspected"
    MADE_BLOCK = "made block"
    ADMIN_LOGIN = "administrator signed in"
    ENROLL_AUTHENTICATOR = "enrolled authenticator"
    RESET_AUTHENTICATOR = "reset authenticator"
    OPERATOR_RESET_AUTHENTICATOR = "operator reset authenticator"
    REQUEST_REFUND = "requested refund"
    CANCEL_PAYMENT = "cancelled unpaid order"
    CANCEL_BOOKING_LINE = "cancelled booking line"
    REMOVED_BLOCK = "removed block"
    # The setup screens : each acts on the booking type, booking
    # category or role it names, the removal's event written before the delete.
    MADE_BOOKING_TYPE = "made booking type"
    CHANGED_BOOKING_TYPE = "changed booking type"
    REMOVED_BOOKING_TYPE = "removed booking type"
    MADE_BOOKING_CATEGORY = "made booking category"
    CHANGED_BOOKING_CATEGORY = "changed booking category"
    REMOVED_BOOKING_CATEGORY = "removed booking category"
    MADE_ROLE = "made role"
    CHANGED_ROLE = "changed role"
    REMOVED_ROLE = "removed role"
    # The assets and spaces screens : each acts on the space,
    # asset, floor plan, spot or group of spots it names; a removal's event is
    # written before the delete, a floor plan's or an asset's before its spots
    # and groups go with it. A spot added with others gets an event of its own.
    MADE_SPACE = "made space"
    CHANGED_SPACE = "changed space"
    REMOVED_SPACE = "removed space"
    MADE_ASSET = "made asset"
    CHANGED_ASSET = "changed asset"
    REMOVED_ASSET = "removed asset"
    MADE_FLOOR_PLAN = "made floor plan"
    CHANGED_FLOOR_PLAN = "changed floor plan"
    REMOVED_FLOOR_PLAN = "removed floor plan"
    MADE_SPOT = "made spot"
    CHANGED_SPOT = "changed spot"
    REMOVED_SPOT = "removed spot"
    MADE_SPOT_GROUP = "made spot group"
    CHANGED_SPOT_GROUP = "changed spot group"
    REMOVED_SPOT_GROUP = "removed spot group"
    # The container screens : a container's change, also enabling
    # or disabling it and its booking categories, the removal's event written
    # before the delete.
    MADE_BOOKING_CONFIGURATION = "made container"
    CHANGED_BOOKING_CONFIGURATION = "changed container"
    REMOVED_BOOKING_CONFIGURATION = "removed container"
    # The price books : a price is the entries one container has
    # in one booking category; its tiers are written with it, under its event.
    MADE_PRICE_BOOK = "made price book"
    CHANGED_PRICE_BOOK = "changed price book"
    REMOVED_PRICE_BOOK = "removed price book"
    MADE_PRICE = "made price"
    CHANGED_PRICE = "changed price"
    REMOVED_PRICE = "removed price"
    # A page of a list that names who booked or a member's contact details
    # , each on the organization.
    LISTED_MEMBERS = "listed members"
    LISTED_BOOKINGS = "listed bookings"
    LISTED_PAYMENTS = "listed payments"
    LISTED_SCHEDULE = "listed schedule"
    # Availability : a weekly availability from a date and a
    # closure by date, each made, changed or removed. A removal's event is
    # written before the delete.
    MADE_AVAILABILITY = "made availability"
    CHANGED_AVAILABILITY = "changed availability"
    REMOVED_AVAILABILITY = "removed availability"
    MADE_CLOSURE = "made closure"
    CHANGED_CLOSURE = "changed closure"
    REMOVED_CLOSURE = "removed closure"
    # A cancel that left money for a person to refund: no refund could be
    # queued for the order. Target: the order.
    LEFT_REFUND_BY_HAND = "left refund by hand"
    # The organization's own details : one event per save,
    # target the organization. It never says which detail changed.
    CHANGED_ORGANIZATION = "changed organization"
    # A payout's page names who booked each payment in it .
    LISTED_PAYOUTS = "listed payouts"
    # The events : an event made, changed (its
    # details, time, asset or contact) or removed, and one day of a repeat
    # removed or given other hours, the day's event naming its date. The
    # daily top-up writes none.
    MADE_EVENT = "made event"
    CHANGED_EVENT = "changed event"
    REMOVED_EVENT = "removed event"
    REMOVED_EVENT_DAY = "removed event day"
    CHANGED_EVENT_DAY = "changed event day"
    # A booking from the organization's earlier calendar, brought in by an
    # administrator settled and with nothing to pay (`old_booking_import`).
    # Target: its order.
    BROUGHT_IN_BOOKING = "brought in old booking"


OBJECT_KINDS = (
    "organization",
    "space",
    # The floor plan, stored in core_assetinventory .
    "asset_inventory",
    "spot_group",
    "spot",
    "asset",
    "availability_calendar",
    "available_span",
    "availability_closure",
    "booking_type",
    "booking_category",
    "booking_configuration",
    "configuration_category",
    "price_book",
    "price_book_entry",
    "price_tier",
    "role",
    "role_category",
    "membership",
    "member",
    "member_membership",
    "member_source",
    "member_import",
    "access_event",
    "login_code",
    "login_attempt",
    "login_request",
    "authenticator",
    "security_notice",
    "order",
    "taken_time",
    "capacity_allocation",
    "booking",
    "booking_line",
    "payment",
    "refund",
    "block",
    "payment_provider_account",
    "audit_event",
    "event",
    "event_day",
)
