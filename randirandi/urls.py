# SPDX-License-Identifier: AGPL-3.0-only
"""The addresses the app answers to."""

from django.urls import path

from randirandi.core import (
    admin_asset_views,
    admin_availability_views,
    admin_container_views,
    admin_event_views,
    admin_price_views,
    admin_setup_views,
    admin_views,
    authenticator_views,
    booking_views,
    event_member_views,
    login_session_views,
    payment_views,
    public_pages,
)
from randirandi.core.login import login
from randirandi.ops import views

handler400 = "randirandi.core.presentation.bad_request"
handler403 = "randirandi.core.presentation.permission_denied"
handler404 = "randirandi.core.presentation.page_not_found"
handler500 = "randirandi.core.presentation.server_error"

urlpatterns = [
    path("booking/choices/", booking_views.choices, name="booking-choices"),
    path(
        "booking/choices/category/",
        booking_views.choices_category,
        name="booking-choices-category",
    ),
    path("booking/calendar/", booking_views.calendar, name="booking-calendar"),
    path("booking/price/", booking_views.price, name="booking-price"),
    path("booking/cart/", booking_views.cart, name="booking-cart"),
    path("booking/cart/add/", booking_views.cart_add, name="booking-cart-add"),
    path(
        "booking/cart/<str:row_id>/change/",
        booking_views.cart_change,
        name="booking-cart-change",
    ),
    path(
        "booking/cart/<str:row_id>/remove/",
        booking_views.cart_remove,
        name="booking-cart-remove",
    ),
    path("booking/cart/empty/", booking_views.cart_empty, name="booking-cart-empty"),
    path("booking/cart/order/", booking_views.cart_order, name="booking-cart-order"),
    path(
        "booking/bookings/",
        booking_views.member_bookings,
        name="booking-bookings",
    ),
    path(
        "booking/order/<uuid:order_public_id>/",
        booking_views.order_outcome,
        name="booking-order",
    ),
    path(
        "booking/order/<uuid:order_public_id>/cancel/",
        booking_views.order_cancel,
        name="booking-order-cancel",
    ),
    path(
        "booking/order/<uuid:order_public_id>/calendar/",
        booking_views.order_calendar,
        name="booking-order-calendar",
    ),
    path(
        "payments/start/<uuid:order_public_id>/",
        payment_views.start,
        name="payment-start",
    ),
    path("payments/return/", payment_views.returned, name="payment-return"),
    path("payments/webhook/", payment_views.webhook, name="payment-webhook"),
    path("events/", login_session_views.events, name="events"),
    path("events/data/", event_member_views.events, name="events-data"),
    path("events/<int:event>/", login_session_views.event, name="event"),
    path("admin/", admin_views.index, name="admin"),
    path("admin/audit/events/", admin_views.audit_events, name="admin-audit-events"),
    path("admin/bookings/list/", admin_views.bookings, name="admin-bookings-list"),
    path("admin/payments/list/", admin_views.payments, name="admin-payments-list"),
    path(
        "admin/payments/payouts/",
        admin_views.payouts_page,
        name="admin-payouts",
    ),
    path(
        "admin/payments/payouts/list/",
        admin_views.payouts_list,
        name="admin-payouts-list",
    ),
    path(
        "admin/payments/payouts/<int:payout>/",
        admin_views.payout_page,
        name="admin-payout",
    ),
    path(
        "admin/payments/payouts/<int:payout>/data/",
        admin_views.payout_data,
        name="admin-payout-data",
    ),
    path("admin/schedule/list/", admin_views.schedule, name="admin-schedule-list"),
    path("admin/blocks/list/", admin_views.blocks, name="admin-blocks-list"),
    path("admin/events/list/", admin_event_views.events_list, name="admin-events-list"),
    path(
        "admin/events/contacts/",
        admin_event_views.contacts,
        name="admin-events-contacts",
    ),
    path(
        "admin/events/in-the-way/",
        admin_event_views.in_the_way,
        name="admin-events-in-the-way",
    ),
    path(
        "admin/events/preview/", admin_event_views.preview, name="admin-events-preview"
    ),
    path("admin/events/make/", admin_event_views.make, name="admin-events-make"),
    path(
        "admin/events/days/<int:day>/remove/",
        admin_event_views.remove_day,
        name="admin-events-day-remove",
    ),
    path(
        "admin/events/days/<int:day>/hours/",
        admin_event_views.day_hours,
        name="admin-events-day-hours",
    ),
    path("admin/events/<int:event>/", admin_views.event_page, name="admin-event"),
    path(
        "admin/events/<int:event>/data/",
        admin_event_views.event_data,
        name="admin-events-data",
    ),
    path(
        "admin/events/<int:event>/change/",
        admin_event_views.change,
        name="admin-events-change",
    ),
    path(
        "admin/events/<int:event>/remove/",
        admin_event_views.remove,
        name="admin-events-remove",
    ),
    path("admin/members/list/", admin_views.members, name="admin-members-list"),
    path("admin/members/add/", admin_views.member_add, name="admin-member-add"),
    path(
        "admin/members/imports/",
        admin_views.imports_page,
        name="admin-member-imports",
    ),
    path(
        "admin/members/imports/list/",
        admin_views.member_imports_list,
        name="admin-member-imports-list",
    ),
    path(
        "admin/members/imports/start/",
        admin_views.member_import_start,
        name="admin-member-import-start",
    ),
    path(
        "admin/members/imports/<int:number>/",
        admin_views.import_page,
        name="admin-member-import",
    ),
    path(
        "admin/members/imports/<int:number>/data/",
        admin_views.member_import_data,
        name="admin-member-import-data",
    ),
    path(
        "admin/members/<int:member>/",
        admin_views.member_page,
        name="admin-member",
    ),
    path(
        "admin/members/<int:member>/data/",
        admin_views.member_data,
        name="admin-member-data",
    ),
    path(
        "admin/members/<int:member>/block/",
        admin_views.member_block,
        name="admin-member-block",
    ),
    path(
        "admin/members/<int:member>/unblock/",
        admin_views.member_unblock,
        name="admin-member-unblock",
    ),
    path(
        "admin/members/<int:member>/correct/",
        admin_views.member_correct,
        name="admin-member-correct",
    ),
    path(
        "admin/members/<int:member>/email/",
        admin_views.member_email,
        name="admin-member-email",
    ),
    path(
        "admin/members/<int:member>/reset/",
        admin_views.member_reset,
        name="admin-member-reset",
    ),
    path(
        "admin/members/<int:member>/export/",
        admin_views.member_export,
        name="admin-member-export",
    ),
    path("admin/types/data/", admin_setup_views.types_data, name="admin-types-data"),
    path(
        "admin/types/<slug:kind>/make/",
        admin_setup_views.item_make,
        name="admin-types-make",
    ),
    path(
        "admin/types/<slug:kind>/<int:pk>/change/",
        admin_setup_views.item_change,
        name="admin-types-change",
    ),
    path(
        "admin/types/<slug:kind>/<int:pk>/move/",
        admin_setup_views.item_move,
        name="admin-types-move",
    ),
    path(
        "admin/types/<slug:kind>/<int:pk>/remove/",
        admin_setup_views.item_remove,
        name="admin-types-remove",
    ),
    path("admin/roles/data/", admin_setup_views.roles_data, name="admin-roles-data"),
    path("admin/roles/make/", admin_setup_views.role_make, name="admin-roles-make"),
    path(
        "admin/roles/<int:pk>/change/",
        admin_setup_views.role_change,
        name="admin-roles-change",
    ),
    path(
        "admin/roles/<int:pk>/remove/",
        admin_setup_views.role_remove,
        name="admin-roles-remove",
    ),
    path(
        "admin/organization/data/",
        admin_setup_views.organization_data,
        name="admin-organization-data",
    ),
    path(
        "admin/organization/change/",
        admin_setup_views.organization_change,
        name="admin-organization-change",
    ),
    path(
        "admin/assets/data/",
        admin_asset_views.place_data,
        name="admin-assets-data",
    ),
    path(
        "admin/assets/plans/<int:plan>/data/",
        admin_asset_views.plan_data,
        name="admin-assets-plan-data",
    ),
    path(
        "admin/assets/spaces/make/",
        admin_asset_views.space_make,
        name="admin-assets-space-make",
    ),
    path(
        "admin/assets/spaces/<int:pk>/change/",
        admin_asset_views.space_change,
        name="admin-assets-space-change",
    ),
    path(
        "admin/assets/spaces/<int:pk>/remove/",
        admin_asset_views.space_remove,
        name="admin-assets-space-remove",
    ),
    path(
        "admin/assets/make/",
        admin_asset_views.asset_make,
        name="admin-assets-make",
    ),
    path(
        "admin/assets/<int:pk>/change/",
        admin_asset_views.asset_change,
        name="admin-assets-change",
    ),
    path(
        "admin/assets/<int:pk>/remove/",
        admin_asset_views.asset_remove,
        name="admin-assets-remove",
    ),
    path(
        "admin/assets/<int:asset>/availability/data/",
        admin_availability_views.data,
        name="admin-availability-data",
    ),
    path(
        "admin/assets/<int:asset>/availability/month/",
        admin_availability_views.month,
        name="admin-availability-month",
    ),
    path(
        "admin/assets/<int:asset>/availability/holidays/",
        admin_availability_views.holidays,
        name="admin-availability-holidays",
    ),
    path(
        "admin/assets/<int:asset>/availability/apply/",
        admin_availability_views.apply,
        name="admin-availability-apply",
    ),
    path(
        "admin/assets/<int:asset>/plans/make/",
        admin_asset_views.plan_make,
        name="admin-assets-plan-make",
    ),
    path(
        "admin/assets/plans/<int:plan>/change/",
        admin_asset_views.plan_change,
        name="admin-assets-plan-change",
    ),
    path(
        "admin/assets/plans/<int:plan>/remove/",
        admin_asset_views.plan_remove,
        name="admin-assets-plan-remove",
    ),
    path(
        "admin/assets/plans/<int:plan>/spots/make/",
        admin_asset_views.spots_make,
        name="admin-assets-spots-make",
    ),
    path(
        "admin/assets/plans/<int:plan>/spots/<int:spot>/change/",
        admin_asset_views.spot_change,
        name="admin-assets-spot-change",
    ),
    path(
        "admin/assets/plans/<int:plan>/spots/<int:spot>/remove/",
        admin_asset_views.spot_remove,
        name="admin-assets-spot-remove",
    ),
    path(
        "admin/assets/plans/<int:plan>/groups/make/",
        admin_asset_views.group_make,
        name="admin-assets-group-make",
    ),
    path(
        "admin/assets/plans/<int:plan>/groups/<int:group>/change/",
        admin_asset_views.group_change,
        name="admin-assets-group-change",
    ),
    path(
        "admin/assets/plans/<int:plan>/groups/<int:group>/remove/",
        admin_asset_views.group_remove,
        name="admin-assets-group-remove",
    ),
    path(
        "admin/containers/data/",
        admin_container_views.containers_data,
        name="admin-containers-data",
    ),
    path(
        "admin/containers/make/",
        admin_container_views.container_make,
        name="admin-containers-make",
    ),
    path(
        "admin/containers/<int:pk>/change/",
        admin_container_views.container_change,
        name="admin-containers-change",
    ),
    path(
        "admin/containers/<int:pk>/categories/",
        admin_container_views.container_categories,
        name="admin-containers-categories",
    ),
    path(
        "admin/containers/<int:pk>/remove/",
        admin_container_views.container_remove,
        name="admin-containers-remove",
    ),
    path("admin/prices/data/", admin_price_views.prices_data, name="admin-prices-data"),
    path(
        "admin/prices/books/make/",
        admin_price_views.book_make,
        name="admin-prices-book-make",
    ),
    path(
        "admin/prices/books/<int:pk>/change/",
        admin_price_views.book_change,
        name="admin-prices-book-change",
    ),
    path(
        "admin/prices/books/<int:pk>/remove/",
        admin_price_views.book_remove,
        name="admin-prices-book-remove",
    ),
    path(
        "admin/prices/containers/<int:container>/<int:category>/save/",
        admin_price_views.prices_save,
        name="admin-prices-save",
    ),
    path(
        "admin/prices/containers/<int:container>/<int:category>/remove/",
        admin_price_views.prices_remove,
        name="admin-prices-remove",
    ),
    path("admin/blocks/lay/", admin_views.blocks_lay, name="admin-blocks-lay"),
    path("admin/blocks/make/", admin_views.blocks_make, name="admin-blocks-make"),
    path(
        "admin/blocks/<int:block>/remove/",
        admin_views.blocks_remove,
        name="admin-blocks-remove",
    ),
    path(
        "admin/bookings/<int:number>/",
        admin_views.booking_page,
        name="admin-booking",
    ),
    path(
        "admin/bookings/<int:number>/data/",
        admin_views.booking_data,
        name="admin-booking-data",
    ),
    path(
        "admin/bookings/<int:number>/cancel/",
        admin_views.booking_cancel,
        name="admin-booking-cancel",
    ),
    path("admin/<slug:place>/", admin_views.place, name="admin-place"),
    path("health/", views.health, name="health"),
    path("robots.txt", views.robots, name="robots"),
    path("login/", login, name="login"),
    path("login/code/", login_session_views.email_code, name="email-code"),
    path(
        "login/authenticator/",
        authenticator_views.authenticator,
        name="authenticator",
    ),
    path(
        "login/authenticator/qr/<int:device_id>/",
        authenticator_views.authenticator_qr,
        name="authenticator-qr",
    ),
    path("logout/", login_session_views.logout, name="logout"),
    path("book/", login_session_views.book, name="book"),
    path("book/cart/", login_session_views.cart, name="cart"),
    path("book/prices/", login_session_views.prices, name="prices"),
    path(
        "book/order/<uuid:order_public_id>/conditions/",
        public_pages.order_conditions_page,
        name="order-conditions",
    ),
    path("prices/", public_pages.prices_page, name="public-prices"),
    path(
        "prices/choices/",
        public_pages.prices_choices,
        name="public-prices-choices",
    ),
    path(
        "prices/calendar/",
        public_pages.prices_calendar,
        name="public-prices-calendar",
    ),
    path("conditions/", public_pages.conditions_page, name="conditions"),
    path(
        "book/order/<uuid:order_public_id>/",
        login_session_views.order,
        name="order",
    ),
    path("me/", login_session_views.me, name="me"),
    path("", login_session_views.home, name="home"),
]
