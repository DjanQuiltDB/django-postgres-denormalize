"""
The migration operation that makes stored denormalized fields equal to the values at the end of their lookup paths.
"""

from django.db import migrations
from django.db.models import Case, Exists, F, OuterRef, Subquery, When

from postgres_denormalize.fields import DenormalizedMixin, IsDistinctFrom, get_denormalized_fields


def lookup_path_value(model, field):
    """
    Return an expression for the value at the end of ``field``'s lookup path for a row of ``model``, starting from the
    row's own first hop. When the first hop points at no row, the expression is the row's current value.

    ``model`` is the leaf, or an excluded model that has a copy of the leaf's field, such as an audit table. Such a
    model has the same first hop column as the leaf, so the same path leads from it. The row of the first hop is read in
    a subquery rather than through a join, because an audit table usually declares its foreign keys without a database
    constraint, and its first hop can point at a row that does not exist anymore.
    """
    chain = field.resolve_chain(model)
    first = chain[0]
    ancestor = first.related_model._base_manager.filter(**{first.target_field.attname: OuterRef(first.attname)})
    rest = '__'.join(hop.name for hop in chain[1:])
    # The subquery reads a key or a column of the ancestor, and the default reads the denormalized column itself. For a
    # foreign key, those are fields of different classes with the same value. Using the field of the column that is
    # written as the output field makes the two types match.
    output_field = field.target_field if field.is_relation else field
    return Case(
        When(Exists(ancestor), then=Subquery(ancestor.values(rest)[:1], output_field=output_field)),
        default=F(field.attname),
        output_field=output_field,
    )


def backfill_denormalized(model, also_fill=None, using=None):
    """
    Correct every denormalized field of ``model`` that differs from its lookup path, with one set-based UPDATE per
    field. Rows that already have the right value are not written. For an excluded model, this does nothing.

    ``also_fill`` maps model classes to lists of field names, and fills those fields on every row of each model, with
    the current value at the end of its own lookup path. It is meant for a new column on an excluded model, such as an
    audit table. ``using`` names the database. Without it, each model is written to the database that the router chooses
    for it.

    The function runs no transaction of its own. To correct several leaves together, wrap the calls in one::

        with transaction.atomic():
            backfill_denormalized(Task)
            backfill_denormalized(Comment)

    :doc:`/guides/drift` describes how to find and correct stale copies. ``BackfillDenormalized`` runs this function in
    a migration; see :doc:`/guides/migrations`.
    """
    for field in get_denormalized_fields(model):
        value = lookup_path_value(model, field)
        model._base_manager.db_manager(using).filter(IsDistinctFrom(F(field.attname), value)).update(
            **{field.attname: value}
        )

    for other, names in (also_fill or {}).items():
        for name in names:
            field = other._meta.get_field(name)
            if not isinstance(field, DenormalizedMixin):
                raise TypeError(
                    f'{other._meta.label}.{name} is not a denormalized field, so it has no path to fill from.'
                )
            other._base_manager.db_manager(using).update(**{field.attname: lookup_path_value(other, field)})


class BackfillDenormalized(migrations.RunPython):
    """
    Set every denormalized field of a leaf model to the value at the end of its lookup path.

    A migration runs this before it installs or changes ``denormalize_set``, in two situations:

    - An existing column becomes a denormalized field. Rows whose stored value differs from the lookup path are
      corrected. The trigger only recomputes a row when the row is written, so without the correction, a row that
      differs and is never written again would keep the wrong value. The rows that match are not written, so they get
      no history rows.
    - A new denormalized field is added. Its leaf rows are filled in the same way. ``also_fill`` fills the copy of the
      field on excluded models that have it, such as an audit table: ``also_fill={'taskaudit': ['project_name']}``.
      Every row of those models gets the current value at the end of its own lookup path. The value that an audit row
      had when it was recorded is not known, so this current value is the closest record of the past.

    Model names in ``also_fill`` are resolved in the migration's own app, unless they are given as
    ``'app_label.model_name'``.

    The correction is one set-based UPDATE per field. It runs on the databases that the routers allow for the model, as
    the ``AddField`` that created the column did: the routers' ``allow_migrate`` receives the model's name as the
    ``model_name`` hint. ``hints`` adds to that hint, and a ``model_name`` in ``hints`` replaces it. When the model is
    given as ``'app_label.model_name'``, the operation passes no ``model_name``, because ``allow_migrate`` receives the
    migration's own app label and the pair would name another model. The models in ``also_fill`` are not part of the
    hint, so they must be on the same databases as the model.

    When the correction of a column can violate a unique constraint, the migration needs a step before this one that
    lists the collisions and raises, because the Postgres error names only the first collision.

    Reversing the migration runs nothing: the corrected value is correct whether the migration is applied or not.
    """

    def __init__(self, model_name, also_fill=None, hints=None):
        self.model_name = model_name
        self.also_fill = dict(also_fill or {})
        # The migration file records only the hints that were passed. The model_name hint is added again each time the
        # operation is built.
        self.given_hints = dict(hints or {})
        router_hints = {} if '.' in model_name else {'model_name': model_name.lower()}
        super().__init__(self.backfill, migrations.RunPython.noop, hints={**router_hints, **self.given_hints})

    def deconstruct(self):
        kwargs = {'model_name': self.model_name}
        if self.also_fill:
            kwargs['also_fill'] = self.also_fill
        if self.given_hints:
            kwargs['hints'] = self.given_hints
        return self.__class__.__qualname__, [], kwargs

    def describe(self):
        return f'Backfill the denormalized fields of {self.model_name}'

    def database_forwards(self, app_label, schema_editor, from_state, to_state):
        # RunPython passes the registry to its function, but not the app label, which backfill needs to find the models.
        self.app_label = app_label
        super().database_forwards(app_label, schema_editor, from_state, to_state)

    def backfill(self, apps, schema_editor):
        # The migration runs on the database of the schema editor, such as the one that `migrate --database` names. When
        # a project migrates several databases in turn, the router's choice for writing the model would send every run
        # to the same database.
        also_fill = {self._get_model(apps, name): fields for name, fields in self.also_fill.items()}
        backfill_denormalized(self._get_model(apps, self.model_name), also_fill, using=schema_editor.connection.alias)

    def _get_model(self, apps, name):
        app_label, _, model_name = name.rpartition('.')
        return apps.get_model(app_label or self.app_label, model_name)


__all__ = ['BackfillDenormalized', 'backfill_denormalized', 'lookup_path_value']
