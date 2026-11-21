# SPDX-License-Identifier: AGPL-3.0-only
"""Settings.

One module for every deployment. What differs between the laptop, the sandbox
and production comes from the environment, and nothing secret is written
here. See environment.example for the names.
"""

import os
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured
from django.db.backends.postgresql.psycopg_any import IsolationLevel
from django.utils.csp import CSP

from randirandi import (
    environment,
    infisical,
    logs,
    mail_configuration,
    payment_configuration,
)

BASE_DIR = Path(__file__).resolve().parent.parent


# What the deployment hands over and, at the platform, the secrets fetched
# from Infisical for this start. Kept here and not in the process environment.
# The name is in lower case so that Django does not take it for a setting.
_environ = infisical.at_start(environment.which(os.environ))


def _required(name: str) -> str:
    value = _environ.get(name, "")
    if not value:
        raise ImproperlyConfigured(f"{name} is not set.")
    return value


# --- The environment and the test switches ---------------------------------

DEPLOYMENT = environment.load(_environ)
ENVIRONMENT = DEPLOYMENT.environment
SWITCHES = DEPLOYMENT.switches
DEBUG = DEPLOYMENT.debug
VIPPS_SECRETS = payment_configuration.Secrets(_environ, ENVIRONMENT)

SECRET_KEY = _required("RANDIRANDI_SECRET_KEY")
# Checked by environment.load above; empty only on a laptop that has none.
AUTHENTICATOR_KEY = _environ.get(environment.AUTHENTICATOR_KEY, "").strip()
ALLOWED_HOSTS = [
    host.strip()
    for host in _required("RANDIRANDI_ALLOWED_HOSTS").split(",")
    if host.strip()
]
ORGANIZATION_DOMAIN = environment.organization_domain(_environ)
DOCUMENTATION_URL = environment.documentation_url(_environ)
CLIENT_ADDRESS_SOURCE = environment.client_address_source(_environ)

# --- The application ---------------------------------------------------------

INSTALLED_APPS = [
    "django.contrib.staticfiles",
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.admin.apps.SimpleAdminConfig",
    # For the database's own rules: no double booking, and lists in a field.
    "django.contrib.postgres",
    "randirandi.ops",
    "randirandi.core",
]

# The member is the user object. It has no password (see the Member model).
AUTH_USER_MODEL = "core.Member"
# Organization-aware passwordless authentication. No global-email
# or password backend is enabled by installing the inspection dependencies.
AUTHENTICATION_BACKENDS = ["randirandi.core.authentication.EmailCodeBackend"]
LOGIN_URL = "/login/"
SESSION_COOKIE_AGE = 30 * 60
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.csp",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ]
        },
    }
]

MIDDLEWARE = [
    # Outermost: every response, whoever answers it, keeps out of search engines.
    "randirandi.ops.middleware.NoIndex",
    # First: the platform's own question to the container, answered before
    # the hostname and HTTPS are asked for.
    "randirandi.ops.middleware.ContainerProbe",
    "randirandi.ops.middleware.PermissionsPolicy",
    # Wrap SecurityMiddleware too: a rejected Host still renders an error shell.
    "django.middleware.csp.ContentSecurityPolicyMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    # Behind the redirect to HTTPS, and before anything is served.
    "randirandi.ops.middleware.AccessGate",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "randirandi.core.authentication.SessionPolicy",
    "django.contrib.auth.middleware.LoginRequiredMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
]

ROOT_URLCONF = "randirandi.urls"
WSGI_APPLICATION = "randirandi.wsgi.application"

# --- The database ------------------------------------------------------------

# The app and the job runner connect as a user that reads and writes rows.
# Migrations connect as the user that owns the tables. Which one this is
# follows from the name the deployment hands over. The password comes with
# the name, for the one run of the migrations and on the laptop; or it is
# the app user's own, from Infisical.
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "HOST": _required("RANDIRANDI_DB_HOST"),
        "PORT": _environ.get("RANDIRANDI_DB_PORT", "5432"),
        "NAME": _required("RANDIRANDI_DB_NAME"),
        "USER": _required("RANDIRANDI_DB_USER"),
        "PASSWORD": _environ.get("RANDIRANDI_DB_PASSWORD")
        or _required("RANDIRANDI_DB_APP_PASSWORD"),
        "CONN_MAX_AGE": 60,
        "CONN_HEALTH_CHECKS": True,
        "OPTIONS": {
            "connect_timeout": 5,
            # Each statement sees what is committed when it starts. It is
            # Postgres' own choice, said here so that it is not changed
            # without a look at what counts on it.
            "isolation_level": IsolationLevel.READ_COMMITTED,
        },
    }
}

# --- Security ----------------------------------------------------------------

# The effective gate password, resolved from the deployment state.
# Explicitly open production ignores a password still retained in Infisical.
ACCESS_GATE_PASSWORD = (
    _environ.get(environment.GATE, "") if SWITCHES.access_gate else ""
)

# Caddy on the laptop, the platform's front door when deployed: it ends TLS
# and tells the app that the request came over HTTPS. Nothing else reaches
# the app from outside.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_SSL_REDIRECT = True
SECURE_HSTS_SECONDS = 31_536_000
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
SECURE_CROSS_ORIGIN_OPENER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"

# The "__Host-" names make a browser keep each cookie to this one address.
SESSION_COOKIE_NAME = "__Host-sessionid"
SESSION_COOKIE_SECURE = True
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_NAME = "__Host-csrftoken"
CSRF_COOKIE_SECURE = True
CSRF_COOKIE_SAMESITE = "Lax"

# Nothing is loaded from another address: stylesheets, fonts, scripts and
# images come from our own. No inline scripts or styles.
SECURE_CSP = {
    "default-src": [CSP.NONE],
    "script-src": [CSP.SELF],
    "style-src": [CSP.SELF, CSP.NONCE],
    "img-src": [CSP.SELF],
    "font-src": [CSP.SELF],
    "connect-src": [CSP.SELF],
    # The web app manifest, so a member can put the pages on a phone's home
    # screen .
    "manifest-src": [CSP.SELF],
    "form-action": [CSP.SELF],
    "base-uri": [CSP.NONE],
    "frame-ancestors": [CSP.NONE],
}
CSRF_FAILURE_VIEW = "randirandi.core.presentation.csrf_failure"

# --- The second container ----------------------------------------------------

# The app's health page at its public address. The second container asks it
# on each call of the platform's schedule, the way a visitor would. Empty: it
# is not asked, as on the laptop.
OUTSIDE_CHECK_ADDRESS = _environ.get("RANDIRANDI_OUTSIDE_CHECK_ADDRESS", "")
if OUTSIDE_CHECK_ADDRESS and not OUTSIDE_CHECK_ADDRESS.startswith("https://"):
    raise ImproperlyConfigured(
        "RANDIRANDI_OUTSIDE_CHECK_ADDRESS is an address that starts with https://."
    )

# Only the private worker receives the public instance identifier. Missing
# credentials produce a monitoring failure, without stopping the website.
BACKUP_CHECK_INSTANCE_ID = _environ.get("RANDIRANDI_BACKUP_CHECK_INSTANCE_ID", "")
BACKUP_CHECK_SECRET_KEY = _environ.get("RANDIRANDI_BACKUP_CHECK_SCW_SECRET_KEY", "")

# --- Static files ------------------------------------------------------------

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
    },
}
# WhiteNoise has no type for the web app manifest .
WHITENOISE_MIMETYPES = {".webmanifest": "application/manifest+json"}

# --- Email -------------------------------------------------------------------

# The sandbox console switch is for fictional members only. Mail is sent for
# real only when every recipient is one of the operator's own addresses
# (RANDIRANDI_SANDBOX_MAIL_TO) or the email of an active member an
# administrator added (an "added member" audit event targets the member, and
# no other member has the address without one); an import's made-up members
# stay on the console. The router exists only while that list is not empty
# (it carries the SMTP settings); the deployed sandbox has the operator's list. Why
# the second rule: administrators add testers one by one, and each must
# get their login codes and booking mails without an environment edit and a
# restart per tester. SMTP credentials come from the same in-memory settings
# source as the database credentials.
MAILERS = mail_configuration.mailers(
    _environ,
    console=SWITCHES.email_to_console,
    sandbox_to=SWITCHES.sandbox_mail_to,
)
# Who every message comes from. Every address a member sees or writes to is
# the organization's own (`contact_email`), never this one.
SENDER = environment.sender(_environ)
DEFAULT_FROM_EMAIL = SENDER.header

# Django's deploy check refuses a mailer that only prints. The check is run
# at every start, so it is told about the one case where printing is meant:
# the test switch, which production cannot have on.
# Email uniqueness is per organization; the backend supplies that boundary.
SILENCED_SYSTEM_CHECKS = ["auth.W004"] + (
    ["mail.E001"] if SWITCHES.email_to_console else []
)

# --- Time and language -------------------------------------------------------

# Servers run in UTC. An organization's time zone comes from its setup.
TIME_ZONE = "UTC"
USE_TZ = True
LANGUAGE_CODE = "en"
USE_I18N = True

# --- Logging -----------------------------------------------------------------

LOGGING = logs.config()
