# SPDX-License-Identifier: AGPL-3.0-only
# The image for the app and the job runner. The same image runs on the laptop,
# in the sandbox and in production. Only the environment differs.
#
# The base images are named in images.env, each pinned to a digest.

ARG PYTHON_IMAGE
ARG UV_IMAGE

FROM ${UV_IMAGE} AS uv

FROM ${PYTHON_IMAGE} AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_CACHE=1 \
    UV_PYTHON_DOWNLOADS=never
WORKDIR /app
# The lock file names every package with its hash. A package that does not
# match is refused.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev
COPY manage.py gunicorn.conf.py LICENSE LICENSE-docs TRADEMARKS.md ./
COPY licenses ./licenses
COPY randirandi ./randirandi
ARG RANDIRANDI_VERSION=dev
ARG RANDIRANDI_REVISION=""
RUN .venv/bin/python -m randirandi.version "$RANDIRANDI_VERSION" "$RANDIRANDI_REVISION"
RUN test -s randirandi/core/static/core/ui/app.js && test -s randirandi/core/static/core/ui/main.css
# Static files are gathered here. The values exist for this one command and
# are not in the image's environment. None of them is a secret.
RUN RANDIRANDI_ENVIRONMENT=sandbox \
    RANDIRANDI_SECRET_KEY=only-for-gathering-static-files \
    RANDIRANDI_ALLOWED_HOSTS=localhost \
    RANDIRANDI_ORGANIZATION_DOMAIN=localhost \
    RANDIRANDI_SENDER=none@localhost \
    RANDIRANDI_DB_HOST=none \
    RANDIRANDI_DB_NAME=none \
    RANDIRANDI_DB_USER=none \
    RANDIRANDI_DB_PASSWORD=none \
    .venv/bin/python manage.py collectstatic --no-input
# The example organization, so that the image can be tried anywhere. A
# client's setup is never in the image: it is loaded from a checkout of the
# operator's repository with `load_setup`, which also reads standard input.
COPY setups/example.toml ./setups/example.toml
# Fictional fixture; its command independently refuses production.
COPY dummy-members.csv ./dummy-members.csv

FROM ${PYTHON_IMAGE} AS runtime
RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --no-create-home --shell /usr/sbin/nologin app
WORKDIR /app
# The code belongs to root and the app runs as another user, so the app
# cannot change its own code.
COPY --from=build /app /app
ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
USER 10001:10001
EXPOSE 8000
CMD ["gunicorn", "randirandi.wsgi:application"]
