"""
The triggers that keep every denormalized field equal to the value at the end of its lookup path.

Three kinds of trigger keep each field correct:

- ``DenormalizeSetTrigger``, named ``denormalize_set``, is a ``BEFORE INSERT OR UPDATE`` row trigger on the leaf's
  table. It computes every denormalized field of the row that is being written, so the column is correct no matter
  which code writes the row: forms, services that build instances directly, the admin, factories, ``loaddata``, raw SQL,
  and the previous release, which is still serving during a rolling deployment.
- ``DenormalizeCascadeTrigger``, named ``denormalize_cascade_<app>_<leaf>``, is an ``AFTER UPDATE`` row trigger on the
  table of each model that a leaf's lookup paths cross. Moving a project to another tenant changes the tenant that
  every task under it should have, and renaming a project changes the name that its tasks copy. Neither writes anything
  to the tasks' table, so ``denormalize_set`` never fires for the tasks. This trigger rewrites them.
- ``DenormalizeFillTrigger``, named ``denormalize_fill_<app>_<leaf>``, is an ``AFTER INSERT`` row trigger on the same
  tables. A transaction can insert a task before its project, because Django defers the foreign key checks to the
  commit. ``denormalize_set`` then finds no project for the task, and this trigger fills the task's copies when the
  project is inserted.

``PostgresDenormalizeConfig.ready()`` calls ``register_triggers``, which derives every kind of trigger from the fields
and registers them with pgtrigger. pgtrigger adds every registered trigger to the migration state of its model, so
``makemigrations`` writes the trigger into a migration exactly as it writes a trigger that is listed in
``Meta.triggers``.
"""

import hashlib

import pgtrigger
import pgtrigger.utils
from django.apps import apps
from django.core.exceptions import FieldDoesNotExist

from postgres_denormalize import conf
from postgres_denormalize.fields import get_denormalized_fields

SET_TRIGGER_NAME = 'denormalize_set'

CASCADE_TRIGGER_PREFIX = 'denormalize_cascade_'

FILL_TRIGGER_PREFIX = 'denormalize_fill_'

# The transaction-local Postgres setting in which ``defer_cascades()`` lists the cascade triggers that it skips, as a
# comma-separated list of their Postgres identifiers. While ``DEFERRABLE_CASCADES`` is on, the WHEN condition of every
# cascade trigger reads this setting.
DEFERRED_CASCADES_SETTING = 'postgres_denormalize.deferred_cascades'

# pgtrigger refuses longer trigger names: it adds the prefix `pgtrigger_` and a hash suffix to form a 63-character
# Postgres identifier.
MAX_TRIGGER_NAME_LENGTH = 47

# The alias prefix for the tables of the hops above the hop that changed. The cascade trigger walks those hops to read
# the value that the leaves get. The prefix differs from the `hop` prefix that the same statement uses for the hops
# below, so each alias names one table.
UPPER_HOP_PREFIX = 'upper'


def quote(name):
    """
    Quote a table or column name, the way Django's Postgres backend quotes it in the statements that create the table.

    Postgres folds a name that is not quoted to lowercase. A table or column whose name has an uppercase letter, or is a
    reserved word, would therefore not be found when the trigger runs. The aliases that the triggers make up themselves,
    such as ``hop0`` and ``leaf``, are lowercase and not reserved, so they are not quoted.

    The name is quoted by pgtrigger's helper, which needs no database connection. The default database of a project can
    be one that cannot quote names, such as Django's dummy backend when the Postgres database has another alias.
    """
    return pgtrigger.utils.quote(name)


def hop_alias(index, prefix='hop'):
    """
    Name the SQL alias of the table at position ``index`` of a lookup path.

    ``hop0`` is the table that the first hop points at, and each following alias is the table that the next hop points
    at. The source column is a column of the last alias, which ``value_alias`` names.
    """
    return f'{prefix}{index}'


def value_alias(chain, prefix='hop'):
    """
    Name the alias of the table that has the source column at the end of the lookup path.
    """
    return hop_alias(len(chain) - 2, prefix)


def build_hop_joins(chain, prefix='hop', join='JOIN'):
    """
    Build the ``FROM`` and ``JOIN`` lines that walk a lookup path, with one alias for each table that the path reaches
    after its first hop.

    Both kinds of trigger build their joins with this function, and the cascade trigger also uses it for a part of the
    path. A lookup that reads a value passes ``join='LEFT JOIN'``, so an empty hop along the path yields NULL instead of
    no row.

    Each table is joined on the column that the hop before it points at. That is the primary key, unless the hop is
    declared with ``to_field``.
    """
    lines = [f'FROM {quote(chain[0].related_model._meta.db_table)} {hop_alias(0, prefix)}']
    for index, hop in enumerate(chain[1:-1], start=1):
        alias = hop_alias(index, prefix)
        lines.append(
            f'{join} {quote(hop.related_model._meta.db_table)} {alias} '
            f'ON {alias}.{quote(hop.target_field.column)} = {hop_alias(index - 1, prefix)}.{quote(hop.column)}'
        )
    return lines


def resolvable_fields(model):
    """
    Return ``(field, chain)`` for every denormalized field of ``model`` whose lookup path resolves, in declaration
    order.

    A field whose path does not resolve is skipped, so no trigger is compiled from it. The developer then sees the
    system check error that the field reports, and not a traceback from pgtrigger.
    """
    resolvable = []
    for field in get_denormalized_fields(model):
        try:
            chain = field.resolve_chain(model)
        except FieldDoesNotExist, ValueError:
            continue
        resolvable.append((field, chain))
    return resolvable


def _path_key(hops):
    """
    Identify a sequence of hops, so that lookup paths that start with the same hops can share one statement.
    """
    return tuple((hop.model._meta.label, hop.name) for hop in hops)


def group_by_ancestor_row(resolvable):
    """
    Group ``(field, chain)`` pairs by the ancestor row that they read from, in the order in which the pairs are given.

    Two fields read the same row when their lookup paths cross the same hops and differ only in their source column, as
    ``project__tenant`` and ``project__name`` do. One lookup then serves both fields.
    """
    groups = {}
    for field, chain in resolvable:
        groups.setdefault(_path_key(chain[:-1]), []).append((field, chain))
    return list(groups.values())


def build_skip_unchanged_sql(resolvable):
    """
    Build the statement that ends the trigger early for an UPDATE that cannot change any denormalized field.

    Every field is computed from the leaf's own first hops. An UPDATE that changes none of them therefore reads the same
    ancestor rows as before. Django assigns each denormalized field to itself on every save, so such an UPDATE also
    changes no copy, and the trigger returns without looking anything up. An UPDATE that writes a copy still recomputes
    every field. That happens when a cascade trigger rewrites a copy, or when ``QuerySet.update()`` assigns one. The
    trigger therefore always overwrites a value that code assigns.

    The condition is nested inside ``TG_OP = 'UPDATE'`` rather than joined to it with ``AND``, because SQL does not
    promise to evaluate ``AND`` from left to right, and ``OLD`` is NULL on an INSERT.
    """
    columns = []
    for field, chain in resolvable:
        for column in (chain[0].column, field.column):
            if column not in columns:
                columns.append(column)
    unchanged = '\n        AND '.join(
        f'OLD.{quote(column)} IS NOT DISTINCT FROM NEW.{quote(column)}' for column in columns
    )
    return '\n'.join(
        [
            "IF TG_OP = 'UPDATE' THEN",
            f'    IF {unchanged} THEN',
            '        RETURN NEW;',
            '    END IF;',
            'END IF;',
        ]
    )


def ancestor_variable(index):
    """
    Name the record variable into which the lookup at position ``index`` reads its ancestor row.
    """
    return f'ancestor{index}'


def build_lookup_sql(index, group):
    """
    Build the statements that read one ancestor row and copy its columns onto the row that is being written.

    The row is read into a variable of the generic type ``record``. Postgres resolves a ``DECLARE`` that names a
    column's type, such as ``%TYPE``, when it creates the function, so such a function cannot be created before the
    table exists. A ``record`` names no table, and Postgres resolves a statement in the function body when the
    statement first runs. The function can therefore be created before the tables that it reads. Tools that copy a
    schema rely on that, because they create every function before any table.

    ``FOUND`` tells the two outcomes of the lookup apart. A row that is found is copied as it is, including NULL values.
    No row is found when the transaction inserts a row before the row that its first hop points at. Django creates
    every foreign key as ``DEFERRABLE INITIALLY DEFERRED``, so Postgres checks the constraint only at commit, and any
    code that inserts the rows in that order can do so, not only a bulk load such as ``loaddata``. The value that the
    insert supplies is then kept. When the ancestor row is inserted later in the transaction, the fill trigger on its
    table rewrites the copy.
    """
    _field, chain = group[0]
    first = chain[0]
    variable = ancestor_variable(index)
    selected = ', '.join(
        f'{value_alias(field_chain)}.{quote(field_chain[-1].column)} AS value{position}'
        for position, (_field, field_chain) in enumerate(group)
    )
    lines = [
        f'SELECT {selected}',
        f'INTO {variable}',
        *build_hop_joins(chain, join='LEFT JOIN'),
        f'WHERE {hop_alias(0)}.{quote(first.target_field.column)} = NEW.{quote(first.column)};',
        'IF FOUND THEN',
        *(
            f'    NEW.{quote(field.column)} := {variable}.value{position};'
            for position, (field, _chain) in enumerate(group)
        ),
        'END IF;',
    ]
    return '\n'.join(lines)


class DenormalizeSetTrigger(pgtrigger.Trigger):
    """
    Computes every denormalized field of a leaf row before the row is inserted or updated.

    ``register_triggers`` creates one instance per leaf model. Two instances are equal when they have the same class and
    the same name. ``AppConfig.ready()`` can run more than once in a test process, and pgtrigger's registry raises
    ``KeyError`` when a second instance with the same name is registered that is not equal to the first.
    """

    name = SET_TRIGGER_NAME
    when = pgtrigger.Before
    operation = pgtrigger.Insert | pgtrigger.Update

    def __eq__(self, other):
        return type(other) is type(self) and other.name == self.name

    def __hash__(self):
        return hash((type(self), self.name))

    def get_declare(self, model):
        groups = group_by_ancestor_row(resolvable_fields(model))
        return [(ancestor_variable(index), 'record') for index in range(len(groups))]

    def get_func(self, model):
        resolvable = resolvable_fields(model)
        if not resolvable:
            return 'RETURN NEW;'
        lookups = [build_lookup_sql(index, group) for index, group in enumerate(group_by_ancestor_row(resolvable))]
        return '\n'.join([build_skip_unchanged_sql(resolvable), *lookups, 'RETURN NEW;'])


def build_moved_value_sql(chain, hop_index):
    """
    Build the SQL expression for the value that the leaves below a changed column get, read from the new row of the
    column's owner.

    When the changed column is the source column, the value is that column of the new row. Otherwise it is a scalar
    subquery that walks the remaining hops from the new row, under the ``upper`` aliases. The subquery uses left joins,
    so that an empty hop along the path yields NULL.
    """
    if hop_index == len(chain) - 1:
        return f'NEW.{quote(chain[hop_index].column)}'
    upper = chain[hop_index:]
    lines = [
        f'(SELECT {value_alias(upper, UPPER_HOP_PREFIX)}.{quote(upper[-1].column)}',
        *(f' {line}' for line in build_hop_joins(upper, UPPER_HOP_PREFIX, join='LEFT JOIN')),
        f' WHERE {hop_alias(0, UPPER_HOP_PREFIX)}.{quote(upper[0].target_field.column)}'
        f' = NEW.{quote(upper[0].column)})',
    ]
    return '\n'.join(lines)


def build_cascade_update_sql(leaf, assignments):
    """
    Build the statement that rewrites denormalized fields on the leaf rows below a row that an UPDATE changed or an
    INSERT added.

    ``assignments`` lists ``(field, chain, hop_index)`` triples: each field to rewrite, its resolved lookup path, and
    the position in that path of the column that belongs to the trigger's table. Every triple reaches that table through
    the same hops, so one statement writes all of them, and a leaf row whose copies all change is written once rather
    than once per field. ``NEW`` is the row that the UPDATE or INSERT wrote. The leaf rows below it are the rows whose
    first hop leads to that row.
    When the changed column is the second segment of the path, the first hop points at the row directly. Otherwise the
    statement joins the tables of the hops in between, under the ``hop`` aliases.

    The final ``WHERE`` condition keeps only leaf rows in which some copy differs from the value that is about to be
    written. Without it, moving a task to another project *of the same tenant* would rewrite every comment under the
    task with the tenant that the comment already has, and a history table that tracks the comments would get a row for
    each of them.

    Leaf rows that a soft-delete library has only marked as deleted are rewritten too. If they kept the old value,
    restoring one would bring back a value that differs from its lookup path.
    """
    values = [(field, build_moved_value_sql(chain, hop_index)) for field, chain, hop_index in assignments]

    _field, chain, hop_index = assignments[0]
    # The column of the new row that the hop below it points at: the primary key, unless that hop has a to_field.
    owner_key_column = quote(chain[hop_index - 1].target_field.column)
    if hop_index == 1:
        joins = []
        reaches_new_row = f'leaf.{quote(chain[0].column)} = NEW.{owner_key_column}'
    else:
        below = chain[:hop_index]
        joins = build_hop_joins(below)
        reaches_new_row = (
            f'leaf.{quote(chain[0].column)} = {hop_alias(0)}.{quote(chain[0].target_field.column)}\n'
            f'  AND {hop_alias(hop_index - 2)}.{quote(below[-1].column)} = NEW.{owner_key_column}'
        )

    assigned = ',\n    '.join(f'{quote(field.column)} = {value}' for field, value in values)
    differs = [f'leaf.{quote(field.column)} IS DISTINCT FROM {value}' for field, value in values]
    differs_sql = differs[0] if len(differs) == 1 else '(' + '\n    OR '.join(differs) + ')'

    lines = [
        f'UPDATE {quote(leaf._meta.db_table)} leaf',
        f'SET {assigned}',
        *joins,
        f'WHERE {reaches_new_row}',
        f'  AND {differs_sql};',
    ]
    return '\n'.join(lines)


def build_changed_columns_sql(columns):
    """
    Build the condition that is true for a row in which any of ``columns`` changed.
    """
    return ' OR '.join(f'OLD.{quote(column)} IS DISTINCT FROM NEW.{quote(column)}' for column in columns)


def build_not_deferred_sql(pgid):
    """
    Build the condition that is true unless the current transaction lists the trigger ``pgid`` in
    ``DEFERRED_CASCADES_SETTING``.

    ``current_setting`` returns NULL for a setting that this session never set, and an empty string after a transaction
    that set it has ended. ``string_to_array`` turns NULL into NULL, which would make the whole condition NULL, and the
    trigger would never fire. ``COALESCE`` therefore treats NULL as not deferred.
    """
    listed = f"'{pgid}' = ANY(string_to_array(current_setting('{DEFERRED_CASCADES_SETTING}', true), ','))"
    return f'NOT COALESCE({listed}, false)'


def cascade_trigger_name(leaf_label, prefix=CASCADE_TRIGGER_PREFIX):
    """
    Name the cascade trigger for the leaf ``leaf_label``: ``denormalize_cascade_<app_label>_<model_name>``. The fill
    trigger passes its own ``prefix``.

    When the app label contains an underscore, two labels can give the same name: ``a_b.C`` and ``a.B_C`` both give
    ``<prefix>a_b_c``. A hash of the label then follows the name, so the name is unique, and it still depends only on
    the label. When the name would exceed pgtrigger's limit, it is cut to the length that fits and followed by the hash
    as well.
    """
    app_label, _, model_name = leaf_label.partition('.')
    name = f'{prefix}{app_label.lower()}_{model_name.lower()}'
    if '_' not in app_label and len(name) <= MAX_TRIGGER_NAME_LENGTH:
        return name
    digest = hashlib.sha1(leaf_label.lower().encode(), usedforsecurity=False).hexdigest()[:8]
    return f'{name[: MAX_TRIGGER_NAME_LENGTH - len(digest) - 1]}_{digest}'


class OwnerTrigger(pgtrigger.Trigger):
    """
    The base class of the triggers on the table of an owner: a model that a leaf's lookup paths cross after the first
    hop.

    Each instance serves exactly one leaf, named by ``leaf_label``, and its body writes only that leaf's table. A model
    that the lookup paths of several leaves cross has one instance per leaf. Each trigger body therefore follows from
    the declarations of its own leaf and from nothing else. Adding a field to a leaf changes that leaf's triggers and
    does not add new ones.

    The owner can be a proxy model, when a hop points at the proxy. Its fields, and therefore the hops, belong to the
    concrete model, so a hop is matched to the owner by the concrete model.

    Two instances are equal when they have the same class and serve the same leaf, for the same reason as for
    ``DenormalizeSetTrigger``.
    """

    name_prefix = CASCADE_TRIGGER_PREFIX

    def __init__(self, *, leaf_label, **kwargs):
        self.leaf_label = leaf_label
        super().__init__(name=cascade_trigger_name(leaf_label, self.name_prefix), **kwargs)

    def __eq__(self, other):
        return type(other) is type(self) and other.leaf_label == self.leaf_label

    def __hash__(self):
        return hash((type(self), self.leaf_label))

    def _leaf_and_assignment_groups(self, model):
        """
        Return the leaf, and its ``(field, chain, hop_index)`` triples grouped by the hops that lead from the leaf to
        ``model``.

        A triple names a denormalized field whose lookup path has a segment that belongs to ``model``, and the position
        of that segment.
        """
        # The leaf is looked up through the registry that `model` belongs to. When makemigrations compiles the trigger,
        # that is the registry of the migration state.
        leaf = model._meta.apps.get_model(self.leaf_label)
        table_label = model._meta.concrete_model._meta.label
        groups = {}
        for field, chain in resolvable_fields(leaf):
            for index, hop in enumerate(chain):
                if index and hop.model._meta.concrete_model._meta.label == table_label:
                    groups.setdefault(_path_key(chain[:index]), []).append((field, chain, index))
        return leaf, list(groups.values())

    @staticmethod
    def _owned_columns(group):
        columns = []
        for _field, chain, index in group:
            if chain[index].column not in columns:
                columns.append(chain[index].column)
        return columns


class DenormalizeCascadeTrigger(OwnerTrigger):
    """
    Rewrites a leaf's denormalized fields after an UPDATE on the table of a model that the leaf's lookup paths cross.

    The trigger is row-level. Its ``WHEN`` condition tests every column of its table that one of the leaf's lookup paths
    reads, so Postgres calls its function only for a row in which one of those columns changed. Every other UPDATE on
    the table costs nothing beyond evaluating the condition. The price is that an UPDATE that changes many rows at once
    runs the function once per row, and each run is an indexed UPDATE on the leaf. Frequent bulk updates of such columns
    are therefore a reason not to denormalize them, unless the batch runs inside ``ignore_cascades()`` or
    ``defer_cascades()``.

    While ``DEFERRABLE_CASCADES`` is on, which is the default, the condition also requires that the current transaction
    does not list the trigger in ``DEFERRED_CASCADES_SETTING``. ``defer_cascades()`` lists it there, so Postgres does
    not call the function at all for the rows that a batch changes. When the setting is off, the condition only tests
    the columns. Changing the setting changes the compiled condition of every cascade trigger, so ``makemigrations``
    recreates all of them.
    """

    when = pgtrigger.After
    operation = pgtrigger.Update

    def get_condition(self, model):
        _leaf, groups = self._leaf_and_assignment_groups(model)
        columns = []
        for group in groups:
            columns.extend(column for column in self._owned_columns(group) if column not in columns)
        changed = build_changed_columns_sql(columns)
        if not conf.get_setting('DEFERRABLE_CASCADES'):
            return pgtrigger.Condition(changed)
        # pgtrigger adds parentheses around a condition only when it does not already start with a parenthesis, so the
        # whole condition is wrapped here, and not just the test on the columns.
        return pgtrigger.Condition(f'(({changed}) AND {build_not_deferred_sql(self.get_pgid(model))})')

    def get_func(self, model):
        leaf, groups = self._leaf_and_assignment_groups(model)
        all_columns = {column for group in groups for column in self._owned_columns(group)}
        statements = []
        for group in groups:
            statement = build_cascade_update_sql(leaf, group)
            columns = self._owned_columns(group)
            # The WHEN condition already guarantees that one of this table's columns changed. Only when several groups
            # share the trigger does each group need its own test, so that it does not rewrite rows whose copies cannot
            # differ.
            if set(columns) != all_columns:
                indented = '\n'.join(f'    {line}' for line in statement.split('\n'))
                statement = f'IF {build_changed_columns_sql(columns)} THEN\n{indented}\nEND IF;'
            statements.append(statement)
        return '\n'.join([*statements, 'RETURN NULL;'])


class DenormalizeFillTrigger(OwnerTrigger):
    """
    Rewrites a leaf's denormalized fields after an INSERT on the table of a model that the leaf's lookup paths cross.

    Django creates every foreign key as ``DEFERRABLE INITIALLY DEFERRED``, so a transaction can insert a leaf row before
    the ancestor row that its path reaches. The set trigger then finds no ancestor row and keeps the value that the
    insert supplies. This trigger, named ``denormalize_fill_<app>_<leaf>``, corrects that value when the ancestor row is
    inserted. Its function runs the same statements as the cascade trigger, one for each group of copies. A new row has
    no leaf rows below it in the usual order of inserts, so each statement is one indexed lookup that writes nothing.

    The trigger has no ``WHEN`` condition, because every column of a new row is new, and the batch helpers do not skip
    it.
    """

    name_prefix = FILL_TRIGGER_PREFIX
    when = pgtrigger.After
    operation = pgtrigger.Insert

    def get_func(self, model):
        leaf, groups = self._leaf_and_assignment_groups(model)
        return '\n'.join([*(build_cascade_update_sql(leaf, group) for group in groups), 'RETURN NULL;'])


def owners_of(resolvable):
    """
    Return the owners of a leaf's triggers, in label order: one model for each table that the lookup paths in
    ``resolvable`` cross after the first hop.

    The owner of a table is the model that the hop before it points at. When the hops of the leaf reach one table both
    through a proxy model and through the concrete model, or through several proxy models, the first proxy model in
    label order owns the triggers. A table gets one trigger of each kind per leaf, and a project that points a hop at a
    proxy model wants the triggers in the proxy model's app.
    """
    candidates = {}
    for _field, chain in resolvable:
        for index in range(1, len(chain)):
            owner = chain[index - 1].related_model
            candidates.setdefault(owner._meta.concrete_model._meta.label, set()).add(owner)
    owners = [
        min(models, key=lambda model: (not model._meta.proxy, model._meta.label)) for models in candidates.values()
    ]
    return sorted(owners, key=lambda model: model._meta.label)


def register_triggers():
    """
    Register the three kinds of trigger for every denormalized field in the live model registry.

    Every leaf model gets one ``DenormalizeSetTrigger``. Every model that the leaf's lookup paths cross after the first
    hop gets one ``DenormalizeCascadeTrigger`` and one ``DenormalizeFillTrigger`` for that leaf. The leaves are visited
    in label order, so the triggers are added to the migration state, and to the ``makemigrations`` output, in the same
    order on every run.

    pgtrigger appends a registered trigger to ``Meta.triggers`` and to the migration state of its model only while
    ``PGTRIGGER_MIGRATIONS`` is on. System check ``postgres_denormalize.E301`` requires that.

    Calling this function a second time changes nothing. The trigger classes compare equal by value, and pgtrigger's
    registry ignores a trigger that is equal to a trigger that it already has under the same name.
    """
    for leaf in sorted(apps.get_models(), key=lambda model: model._meta.label):
        register_triggers_for(leaf)


def register_triggers_for(leaf):
    """
    Register the three kinds of trigger for the denormalized fields of one leaf model.

    A field whose lookup path does not resolve is skipped, and the field reports it as a system check error. When none
    of the leaf's fields resolves, the leaf gets no trigger at all.
    """
    resolvable = resolvable_fields(leaf)
    if not resolvable:
        return
    DenormalizeSetTrigger().register(leaf)
    for owner in owners_of(resolvable):
        DenormalizeCascadeTrigger(leaf_label=leaf._meta.label).register(owner)
        DenormalizeFillTrigger(leaf_label=leaf._meta.label).register(owner)


def cascade_triggers_on(owners):
    """
    Return ``(owner, leaf, trigger)`` for every cascade trigger on the tables of the models in ``owners``, in label
    order.

    ``owner`` is the model that the trigger is registered on. That is a proxy model when a hop points at the proxy, and
    the caller can name either the proxy or the concrete model.

    The triggers are read from pgtrigger's registry, where ``register_triggers`` put them, so no lookup path is resolved
    again. A model that has no cascade trigger raises ``ValueError``. No lookup path reads one of its columns after a
    first hop, so naming it is a mistake. Usually a leaf was named instead of its owner.
    """
    registered = [
        (model, trigger) for model, trigger in pgtrigger.registered() if isinstance(trigger, DenormalizeCascadeTrigger)
    ]
    found = []
    for owner in sorted(set(owners), key=lambda model: model._meta.label):
        table_label = owner._meta.concrete_model._meta.label
        on_owner = [
            (model, apps.get_model(trigger.leaf_label), trigger)
            for model, trigger in sorted(registered, key=lambda pair: pair[1].leaf_label)
            if model._meta.concrete_model._meta.label == table_label
        ]
        if not on_owner:
            raise ValueError(
                f'{owner._meta.label} has no cascade trigger: no lookup path reads one of its columns after a first '
                f'hop. Name the models whose rows the batch updates, not the leaves that copy from them.'
            )
        found.extend(on_owner)
    # A caller that names both a proxy model and its concrete model finds the same triggers twice.
    return list(dict.fromkeys(found))


__all__ = [
    'DEFERRED_CASCADES_SETTING',
    'DenormalizeCascadeTrigger',
    'DenormalizeFillTrigger',
    'DenormalizeSetTrigger',
    'OwnerTrigger',
    'cascade_trigger_name',
    'cascade_triggers_on',
    'owners_of',
    'register_triggers',
    'register_triggers_for',
    'resolvable_fields',
]
