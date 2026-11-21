<!-- SPDX-License-Identifier: CC-BY-SA-4.0 -->
# Running RandiRandi

## Local prerequisites

Install Git, Docker with Compose, Node 22.12+, OpenSSL and `uv`. The Python
version is 3.14. The build installs the locked frontend dependencies in a
temporary directory and removes it afterwards.

Keep real member data and production credentials out of this exercise.
`environment.example` documents settings; it is not a credential file to commit.

## Get the source

```sh
git clone https://github.com/Randibooking/randirandi.git
cd randirandi
```

## One-command fictional example

From the repository directory, run:

```sh
bin/example
```

This builds and starts a separate `randirandi-example` Docker Compose project,
loads Harbour View and the bundled fictional members, and makes
`ola.nordmann@example.com` its first administrator. It listens only on this
machine at `https://harbourview.localhost:8445/`. No SMTP or Vipps credentials
are inherited. Read login codes with `bin/example logs`; administrator
sign-in still requires authenticator enrollment. `mari.testesen@example.com`
is a member account; the administrator can try the free `internal` category.

Verify the example's HTTPS endpoint with its public local CA:

```sh
bin/example certificate > local-ca.crt
curl --cacert ./local-ca.crt --resolve harbourview.localhost:8445:127.0.0.1 https://harbourview.localhost:8445/login/
```

For browser use, import this CA following [Caddy's Local HTTPS with Docker
instructions](https://caddyserver.com/docs/running#local-https-with-docker), then
open `https://harbourview.localhost:8445/` and use the fictional accounts above.
Nothing installs a trust anchor or changes the browser's certificate checks
for you. Reset creates a new CA: export and trust that replacement before the
next browser visit; remove the old example CA from your trust store.

```sh
bin/example status
bin/example stop       # keep this example and its generated keys
bin/example start      # resume it
bin/example reset      # erase this fictional example and create it again
bin/example destroy    # erase its containers and volumes
```

Reset/destroy first verify ownership labels on every resource of this named
project. An unrelated project with that name is refused. Other stacks are not
selected. Generated keys stay in this example's local container configuration;
there is no plaintext credential file. Use `stop`, not manual container
removal, to retain them. If only orphaned volumes remain, start refuses and
asks for a deliberate reset.

The example builds from source and runs locally.

## Alternative: manage the local stack yourself

```sh
bin/setup
export RANDIRANDI_ALLOWED_HOSTS=harbourview.localhost,localhost
export RANDIRANDI_ORGANIZATION_DOMAIN=localhost
export RANDIRANDI_SITE_ADDRESS=harbourview.localhost
export RANDIRANDI_EMAIL_TO_CONSOLE=1
export RANDIRANDI_MADE_UP_MEMBERS=1
bin/dev up --build -d
bin/dev exec app python manage.py load_setup setups/example.toml --check
bin/dev exec app python manage.py load_setup setups/example.toml
bin/dev exec app python manage.py import_dummy_members harbourview
```

Before the first browser visit, extract Caddy's public local CA and verify HTTPS:

```sh
bin/dev cp caddy:/data/caddy/pki/authorities/local/root.crt ./local-ca.crt
curl --cacert ./local-ca.crt --resolve harbourview.localhost:8443:127.0.0.1 https://harbourview.localhost:8443/login/
```

For browser use, import that CA following [Caddy's Local HTTPS with Docker
instructions](https://caddyserver.com/docs/running#local-https-with-docker), which
cover operating-system and separate browser trust stores. Use the CA from this
local stack; no application setting disables certificate verification.
`local-ca.crt` is public certificate material and may be removed after the exercise.

After importing the CA, open `https://harbourview.localhost:8443/`.
Read the application log with `bin/dev logs -f app` for the fictional member's
email code. Console delivery is sandbox-only. It is not a test of real SMTP.
Use an address from the bundled fictional fixture. Phone input identifies the
member; the code still arrives by email.

To find a fictional member's ID, run:

```sh
bin/dev exec app python manage.py shell -c 'from randirandi.core.models import Member, Organization; org = Organization.objects.get(subdomain="harbourview"); print(list(Member.objects.of(org).order_by("pk").values_list("pk", "email")))'
bin/dev exec app python manage.py first_administrator harbourview MEMBER_ID 'Administrator'
```

Replace `MEMBER_ID` with an ID printed by the first command. The role is exactly
`Administrator`; `Board` is not an Admin role in this fixture. Set up the
authenticator at first administrator sign-in. Afterwards, in Book choose the
`internal` category and `Function room`, a future weekday between 10:00 and
22:00, with a 1–12-hour period. This fixture's internal price is zero; review
that zero total and confirm it without a payment provider. Check the confirmed
reference in My page. This is a fictional local exercise, not a real booking.


Paid offers need your own Vipps test agreement and provider configuration.
Console delivery is not real SMTP. Production refuses the sandbox console
and made-up-member switches.

## Build and verification

`bin/styles` produces `randirandi/core/static/core/ui/` from the frontend lock.
The directory is ignored by Git. `bin/styles --check` independently rebuilds
and compares the output. `bin/dev build` and `bin/dev up --build` prepare it;
a direct Docker build must run `bin/styles` first or the Dockerfile refuses it.

`bin/check` builds the browser output, runs all
frontend tests, verifies the Python lock, formatting/lint, types for macOS and
Linux, catalogues, licences, secrets and database-file guard, then runs backend
tests against its own disposable PostgreSQL. Docker must already be available.
It does not borrow a deployed database. `bin/check --self-test` exercises
intentional refusal cases in the checks.

`bin/build` checks first and builds the app, Caddy and database-bootstrap images. A clean Git
revision is used as the image tag; an uncommitted build is marked `dev`.
A startup timing measures the first answering application with the example
organization. A full-check timing measures the complete verification command;
these are distinct measurements and neither is a promised duration.

## Configuration and persistence

Set `RANDIRANDI_DOCUMENTATION_URL` to an available absolute HTTP(S) Help
address to change the Documentation link on login, member, administrator and
error pages. Empty or unset keeps the existing destination. This setting
does not publish Help or test that a remote address is available.

The local Compose stack runs Caddy, the app, jobs, migrations and PostgreSQL.
`bin/dev` creates temporary sandbox credentials in process memory when they
are absent. Supply stable credentials through your secret manager for a
persistent environment. Authenticator-key loss requires administrator reset;
keep that key recoverable separately from database backups.

The app runs as an unprivileged OS user with a restricted database login.
Schema migration uses the owner role, then reapplies application privileges.
See `environment.example`, `docker/db/roles.sql` and
`randirandi/ops/privileges.py` for the exact boundary.

```sh
bin/dev ps
bin/dev logs -f app jobs
bin/dev down
```

`down` stops containers. `down -v` also destroys this local stack's volumes;
use it only for a disposable exercise. Do not use these instructions against
an existing production deployment.

## Operating your own instance

Use your own domain, sender, brand and provider agreements. Configure TLS,
allowed hosts, SMTP, database owner/application credentials, authenticator
encryption, Vipps credentials and public callback addresses. Keep sandbox and
production separate. The image contains the fictional example setup only;
load your own configuration separately from a protected source.

Back up and prove restoration of both data and required keys, verify effective
database privileges after migration/restore, run the job worker, and monitor
payment/refund follow-up. These responsibilities belong to the operator.
Source availability, dependency updates and privacy obligations continue after
installation. A working local exercise is not production-readiness evidence.
