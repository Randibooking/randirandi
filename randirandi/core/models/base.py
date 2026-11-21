# SPDX-License-Identifier: AGPL-3.0-only
"""What every object of an organization shares.

Every row belongs to one organization, and a link never crosses from one
organization to another. Two things here keep that so:

- A query has to name its organization. `Asset.objects.of(organization)` is
  the way in, and `Asset.objects.all()` fails. Code with no organization to
  go by, such as the job runner, says so: `Asset.across_organizations`. The
  files that do are listed, each with its reason, in
  `tests/test_ways_around.py`.
- `SameOrganization` makes the database itself refuse a link from one
  organization's row to another's.
"""

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from django.core.exceptions import ValidationError
from django.db import DEFAULT_DB_ALIAS, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor
from django.db.models.constraints import BaseConstraint

if TYPE_CHECKING:
    from randirandi.core.models.members import Member
    from randirandi.core.models.organization import Organization

DAY = 24 * 60
"""Minutes in a day. Times of day are held as minutes after midnight."""


WEEK = 7 * DAY
"""Minutes in a week. The model is planned on a seven-day week."""


def clock(minutes: int) -> str:
    """A time of day as people read it: 1020 is 17:00."""
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def length(minutes: int) -> str:
    """A length as people read it: 240 is 4 h, and 90 is 90 min."""
    return f"{minutes // 60} h" if minutes % 60 == 0 else f"{minutes} min"


class OrganizationNotNamedError(Exception):
    """A query did not say which organization it is for."""


class OrganizationManager[T: "Owned"](models.Manager[T]):
    """Reads one organization's rows, and refuses to read without one."""

    def get_queryset(self) -> models.QuerySet[T]:
        name = self.model.__name__
        raise OrganizationNotNamedError(
            f"{name}.objects needs an organization: {name}.objects.of(organization)."
        )

    def of(self, organization: Organization | int) -> models.QuerySet[T]:
        """The rows of one organization."""
        return super().get_queryset().filter(organization=organization)

    def create(self, **fields: Any) -> T:
        """Make one row. Its organization is among the fields."""
        row = self.model(**fields)
        if row.organization_id is None:
            raise OrganizationNotNamedError(
                f"A new {self.model.__name__} needs its organization."
            )
        row.save(force_insert=True, using=self.db)
        return row


class MemberManager[T: "Owned"](OrganizationManager[T]):
    """For an object that is one member's own, such as an order: it also
    reads one member's rows. What a member's pages show starts here.
    """

    def of_member(self, member: Member) -> models.QuerySet[T]:
        """The member's own rows, and nobody else's."""
        return self.of(member.organization_id).filter(member=member)


def _stored(model: type[models.Model], name: str) -> models.Field[Any, Any]:
    """A field of the model that is a column of its table."""
    field = model._meta.get_field(name)
    if not isinstance(field, models.Field):
        raise TypeError(f"{model.__name__}.{name} is not a column.")
    return field


def _column(model: type[models.Model], name: str) -> str:
    """The name of the column that holds one of the model's fields."""
    column: str | None = _stored(model, name).column
    if column is None:
        raise TypeError(f"{model.__name__}.{name} is not a column.")
    return column


class SameOrganization(BaseConstraint):
    """A link points to a row of the same organization.

    In the database this is a second foreign key beside the ordinary one,
    over the organization and the link together. The table it points to is
    unique over its organization and id, which `organization_rules` gives
    every table.

    `organization` names the field that holds the row's own organization. It
    is "id" on the organization itself.
    """

    default_violation_error_message = "It belongs to another organization."

    def __init__(
        self, *, link: str, name: str, organization: str = "organization"
    ) -> None:
        self.link = link
        self.organization = organization
        super().__init__(name=name)

    def _create_statement(
        self, model: type[models.Model], schema_editor: BaseDatabaseSchemaEditor
    ) -> str:
        quote = schema_editor.quote_name
        target = _stored(model, self.link).related_model
        if not isinstance(target, type):
            raise TypeError(f"{self.name}: {self.link} is not a link to one object.")
        return (
            f"ALTER TABLE {quote(model._meta.db_table)} "
            f"ADD CONSTRAINT {quote(self.name)} "
            f"FOREIGN KEY ({quote(_column(model, self.organization))}, "
            f"{quote(_column(model, self.link))}) "
            f"REFERENCES {quote(target._meta.db_table)} "
            f"({quote(_column(target, 'organization'))}, "
            f"{quote(_column(target, 'id'))})"
        )

    # A table may be made before the table its link points to. So the foreign
    # key is added when the migration has made every table, in both cases
    # below: with the table, and to a table that exists.

    def constraint_sql(
        self,
        model: type[models.Model] | None,
        schema_editor: BaseDatabaseSchemaEditor | None,
    ) -> str:
        assert model is not None and schema_editor is not None  # noqa: S101
        schema_editor.deferred_sql.append(self._create_statement(model, schema_editor))
        return ""

    def create_sql(
        self,
        model: type[models.Model] | None,
        schema_editor: BaseDatabaseSchemaEditor | None,
    ) -> str:
        return self.constraint_sql(model, schema_editor)

    def remove_sql(
        self,
        model: type[models.Model] | None,
        schema_editor: BaseDatabaseSchemaEditor | None,
    ) -> str:
        assert model is not None and schema_editor is not None  # noqa: S101
        quote = schema_editor.quote_name
        return (
            f"ALTER TABLE {quote(model._meta.db_table)} "
            f"DROP CONSTRAINT {quote(self.name)}"
        )

    def validate(
        self,
        model: type[models.Model],
        instance: models.Model,
        exclude: set[str] | None = None,
        using: str = DEFAULT_DB_ALIAS,
    ) -> None:
        if exclude and (self.link in exclude or self.organization in exclude):
            return
        if getattr(instance, _stored(model, self.link).attname) is None:
            return
        linked = getattr(instance, self.link)
        own = getattr(instance, _stored(model, self.organization).attname)
        if linked.organization_id != own:
            raise ValidationError(
                {self.link: self.get_violation_error_message()},
                code=self.violation_error_code,
            )

    def __eq__(self, other: object) -> bool:
        if isinstance(other, SameOrganization):
            return (
                self.name == other.name
                and self.link == other.link
                and self.organization == other.organization
            )
        return NotImplemented

    def __hash__(self) -> int:
        return hash((self.name, self.link, self.organization))

    def __repr__(self) -> str:
        return f"<SameOrganization: link={self.link!r} name={self.name!r}>"

    def deconstruct(self) -> tuple[str, Sequence[Any], dict[str, Any]]:
        path, args, kwargs = super().deconstruct()
        kwargs["link"] = self.link
        if self.organization != "organization":
            kwargs["organization"] = self.organization
        return path, args, kwargs


class DatabaseRule(BaseConstraint):
    """A native PostgreSQL rule that spans columns or uses range expressions.

    Definitions are fixed application source, never user input. Keeping them
    in model state lets migration checks detect drift. PostgreSQL validates
    these rules; model.full_clean() does not substitute for a transaction.
    """

    def __init__(self, *, name: str, definition: str) -> None:
        self.definition = definition
        super().__init__(name=name)

    def constraint_sql(self, model: Any, schema_editor: Any) -> str:
        return f"CONSTRAINT {schema_editor.quote_name(self.name)} {self.definition}"

    def create_sql(self, model: Any, schema_editor: Any) -> str:
        return (
            f"ALTER TABLE {schema_editor.quote_name(model._meta.db_table)} ADD "
            f"{self.constraint_sql(model, schema_editor)}"
        )

    def remove_sql(self, model: Any, schema_editor: Any) -> str:
        return (
            f"ALTER TABLE {schema_editor.quote_name(model._meta.db_table)} "
            f"DROP CONSTRAINT {schema_editor.quote_name(self.name)}"
        )

    def validate(
        self,
        model: Any,
        instance: Any,
        exclude: Any = None,
        using: str = DEFAULT_DB_ALIAS,
    ) -> None:
        pass

    def deconstruct(self) -> tuple[str, Sequence[Any], dict[str, Any]]:
        path, args, kwargs = super().deconstruct()
        kwargs["definition"] = self.definition
        return path, args, kwargs

    def __eq__(self, other: object) -> bool:
        if isinstance(other, DatabaseRule):
            return self.name == other.name and self.definition == other.definition
        return NotImplemented

    def __hash__(self) -> int:
        return hash((self.name, self.definition))


def restrict_updates(
    instance: models.Model,
    kwargs: dict[str, Any],
    *,
    writable: tuple[str, ...],
    identity: tuple[str, ...],
) -> None:
    """Make ordinary ORM saves compatible with the column privilege contract.

    Database grants also protect bulk/direct writes. Compare identity against
    the stored row; stale cascading witnesses are deliberately not written.
    """
    if instance._state.adding:
        return
    using = kwargs.get("using") or instance._state.db or DEFAULT_DB_ALIAS
    stored = type(instance)._base_manager.using(using).get(pk=instance.pk)
    if any(getattr(stored, field) != getattr(instance, field) for field in identity):
        raise ValidationError("An existing row's identity cannot be changed.")
    requested = kwargs.get("update_fields")
    if requested is not None and not set(requested) <= set(writable):
        raise ValidationError("Only the permitted fields can be updated.")
    kwargs["update_fields"] = list(writable) if requested is None else requested


def organization_rules(*links: str) -> list[BaseConstraint]:
    """The constraints every table of an organization has.

    The table is unique over its organization and id, so that other tables
    can point to it with `SameOrganization`. Each of its own links to another
    of the organization's objects is named here and kept inside the
    organization.
    """
    return [
        models.UniqueConstraint(fields=["organization", "id"], name="%(class)s_org_id"),
        *(
            SameOrganization(link=link, name=f"%(class)s_{link}_same_org")
            for link in links
        ),
    ]


class Owned(models.Model):
    """A row that belongs to one organization."""

    organization = models.ForeignKey(
        "core.Organization", on_delete=models.PROTECT, related_name="+"
    )

    across_organizations = models.Manager()
    objects = OrganizationManager()

    class Meta:
        abstract = True
        # Django's own reads, such as following a link, go through this one.
        default_manager_name = "across_organizations"
