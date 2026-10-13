"""
The two kinds of denormalized field, and the helpers that find a model's denormalized fields and check their stored
values.

``DenormalizedForeignKey`` copies a foreign key at the end of a chain of foreign keys. ``DenormalizedField`` copies any
other column of an ancestor row, such as its name. Both share ``DenormalizedMixin``, which has everything that does not
depend on what is copied: the lookup path, the rule that no code writes the column, and reading the value that the
trigger computes back onto the instance.

The lookup path is an argument of the field, not a class attribute of the model. pgtrigger compiles a trigger's SQL
against the model that Django rebuilds from migration state, and that model has fields and ``Meta`` options but no other
class attributes. For the same reason, ``deconstruct`` writes ``denormalize_from`` into every migration that touches the
field.

Both fields infer what they can from the lookup path in model code, such as the target and the ``on_delete`` rule of a
foreign key or the type of a copied column, and write the result into migrations. A field that Django rebuilds from
migration state receives the recorded value, so migrations never depend on the live models.
"""

import operator
from functools import reduce

from django.apps import apps as global_apps
from django.core import checks
from django.core.exceptions import FieldDoesNotExist, ImproperlyConfigured
from django.db import models
from django.db.models import BooleanField, F, Func, Q
from django.db.models.fields.related import lazy_related_operation

from postgres_denormalize import conf
from postgres_denormalize.chains import PathRefused, is_forward_hop, refuse_hop, resolve_chain

# The model reference that a DenormalizedForeignKey passes to ForeignKey.__init__ until the target is known from the
# lookup path. No app can provide a model with this reference, so nothing ever resolves it by accident.
INFERRED_TARGET = 'postgres_denormalize.__inferred_from_path__'


def INFERRED_ON_DELETE(collector, field, sub_objs, using):
    """
    The ``on_delete`` rule that a DenormalizedForeignKey passes to ForeignKey.__init__ until the rule is inferred from
    the lookup path. ForeignKey.__init__ requires a callable. A delete that reaches this rule fails loudly, rather than
    doing nothing to the rows that store the copy.
    """
    raise ImproperlyConfigured(
        f'{field} cannot infer its on_delete rule, because `{field.denormalize_from}` does not resolve, or crosses a '
        f'foreign key with a custom on_delete rule. The system checks say why.'
    )


# The rules of a hop that delete the row that the hop belongs to, when the row that the hop points at is deleted. The
# database variants exist from Django 6.1 on.
CASCADE_RULES = tuple(rule for rule in (models.CASCADE, getattr(models, 'DB_CASCADE', None)) if rule is not None)

# The rules of a hop that the copy takes over unchanged: the copy is set to NULL, or the delete is refused.
COPIED_RULES = tuple(
    rule
    for rule in (models.SET_NULL, getattr(models, 'DB_SET_NULL', None), models.PROTECT, models.RESTRICT)
    if rule is not None
)

# The rules of a hop that keep the row that the hop belongs to, and leave the hop pointing at another row, or at the
# deleted row. The trigger then writes the copy, so the copy itself needs no rule.
REPOINTING_RULES = tuple(
    rule
    for rule in (models.SET_DEFAULT, getattr(models, 'DB_SET_DEFAULT', None), models.DO_NOTHING)
    if rule is not None
)


def is_set_rule(rule):
    """
    Say whether ``rule`` is a rule that ``models.SET(value)`` returns.
    """
    deconstruct = getattr(rule, 'deconstruct', None)
    return deconstruct is not None and deconstruct()[0] == 'django.db.models.SET'


def describe_rule(rule):
    """
    Return the name of an ``on_delete`` rule as a declaration spells it, such as ``CASCADE`` or ``SET(0)``.
    """
    if is_set_rule(rule):
        _path, args, _kwargs = rule.deconstruct()
        return f'SET({args[0]!r})'
    return getattr(rule, '__name__', repr(rule))


def infer_on_delete(chain):
    """
    Return the ``on_delete`` rule that a copy of the foreign key at the end of ``chain`` needs, and the hop whose rule
    decides it. The rule is ``None`` when that hop has a custom rule, which says nothing about what happens to the copy.

    When the row at the end of the lookup path is deleted, the hops decide what happens to a leaf row, starting at the
    hop next to that row. A hop that cascades deletes the row that it belongs to, so the next hop toward the leaf
    decides. The first hop that does not cascade decides for the leaf:

    - ``SET_NULL`` sets that hop to NULL, so the copy becomes NULL too, and the copy gets the same rule.
    - ``PROTECT`` and ``RESTRICT`` refuse the delete, and the copy gets the same rule.
    - ``SET``, ``SET_DEFAULT`` and ``DO_NOTHING`` keep the leaf row. The trigger writes the copy, so the copy gets
      ``DO_NOTHING``.

    When every hop cascades, the leaf row is deleted, and the copy gets the rule of the last hop. The copy's rule is
    therefore never stricter than the path: it deletes, refuses or sets to NULL only the rows that the path would.
    Django requires the hops along a path to use either the Python rules or the database rules, so the copy's rule uses
    the same kind as the path.
    """
    for hop in reversed(chain):
        rule = hop.remote_field.on_delete
        if rule in CASCADE_RULES:
            continue
        if rule in COPIED_RULES:
            return rule, hop
        if rule in REPOINTING_RULES or is_set_rule(rule):
            return models.DO_NOTHING, hop
        return None, hop
    return chain[-1].remote_field.on_delete, chain[-1]


# The keyword arguments of the source column that a DenormalizedField does not copy onto its own column. Each one does
# one of three things. It describes the source as a form or admin input (Field.non_db_attrs). Or it gives the copy a
# constraint or index that the source only needs on its own table (unique, db_index, primary_key). Or it lets the copy
# get a value in some other way than through the trigger (default, db_default, auto_now, auto_now_add). null is inferred
# from the whole lookup path.
OMITTED_SOURCE_KWARGS = frozenset(
    {
        *models.Field.non_db_attrs,
        'auto_now',
        'auto_now_add',
        'db_comment',
        'db_default',
        'db_index',
        'db_tablespace',
        'default',
        'null',
        'primary_key',
        'unique',
    }
)


class IsDistinctFrom(Func):
    """
    ``lhs IS DISTINCT FROM rhs``: true when the two differ, counting NULL as a value of its own.

    ``~Q(column=F(path))`` does not say that. SQL's ``<>`` is unknown when either side is NULL. A filter that is built
    from it therefore misses a copy that has a value while its lookup path ends at NULL, and the other way around.
    """

    template = '(%(expressions)s)'
    arg_joiner = ' IS DISTINCT FROM '
    output_field = BooleanField()


class DenormalizedMixin:
    """
    What every denormalized field shares: a column that stores a copy of the value at the end of ``denormalize_from``,
    and that is written by triggers and never by code.

    On an INSERT, Django sends the columns of the instance. For an ordinary field, it reads nothing back, so the
    instance keeps the value that the caller passed to the constructor. Application code never assigns a denormalized
    field, so that value would be missing. A denormalized field therefore reports ``db_returning``. This makes Django
    add the column to the INSERT's ``RETURNING`` clause and assign the value that the trigger computes to the instance.

    On an UPDATE, Django returns only generated columns and columns that are assigned an expression, never
    ``db_returning`` ones. ``pre_save`` therefore assigns the column to itself as an expression on every update. When
    the row moves under another ancestor, the trigger replaces that value before the row is written. Django reads the
    computed value back from the UPDATE's ``RETURNING`` clause, so the instance has the new value. Two kinds of write do
    not update the instance: a ``save(update_fields=...)`` that does not list the field, and ``QuerySet.update()``,
    which updates no instance at all. After either, the caller calls ``refresh_from_db`` to read the new value.

    The field is never editable, so no ModelForm or admin form offers it as an input whose value the trigger would
    discard. It is always blank, so ``full_clean()`` does not demand a value from code that never assigns one.
    """

    # Whether the lookup path may end at a column that is not a foreign key. Each concrete class sets it.
    copies_column = False

    # A different lookup path produces a different trigger, but it never changes the column itself. Changing
    # `denormalize_from` must therefore not make Django drop and recreate the column's constraints.
    non_db_attrs = (*models.Field.non_db_attrs, 'denormalize_from')

    def __init__(self, *args, denormalize_from, **kwargs):
        self.denormalize_from = denormalize_from
        kwargs['editable'] = False
        kwargs['blank'] = True
        super().__init__(*args, **kwargs)

    def deconstruct(self):
        name, path, args, kwargs = super().deconstruct()
        kwargs['denormalize_from'] = self.denormalize_from
        # __init__ always sets both, so writing them into migrations would only add noise.
        kwargs.pop('editable', None)
        kwargs.pop('blank', None)
        return name, path, args, kwargs

    def pre_save(self, model_instance, add):
        if add:
            return super().pre_save(model_instance, add)
        return F(self.attname)

    @property
    def db_returning(self):
        # Django's own comment calls this private API. If a future Django ignores it, the stored column is still
        # correct, because the trigger computes it. Only the write-back to the instance breaks, which leaves the
        # attribute unset on created instances. The test suite asserts the write-back, so that break shows up as a
        # failing test rather than as an unexplained RelatedObjectDoesNotExist.
        return True

    def resolve_chain(self, model=None):
        """
        Return the fields that the lookup path crosses, starting on ``model``, or by default on the model that declares
        the field.
        """
        return resolve_chain(model or self.model, self.denormalize_from, allow_column_terminal=self.copies_column)

    def check(self, **kwargs):
        return [*super().check(**kwargs), *self._check_denormalize_from()]

    def _check_denormalize_from(self):
        if '__' not in self.denormalize_from:
            return self._error(
                'postgres_denormalize.E001',
                f'`denormalize_from` is `{self.denormalize_from}`, which is a column on the same row, not a column '
                'that is reached through a foreign key.',
            )

        try:
            chain = self.resolve_chain()
        except PathRefused as refusal:
            return self._error(refusal.code, f'{refusal}.', refusal.hint)
        except (FieldDoesNotExist, ValueError) as exception:
            return self._error(
                'postgres_denormalize.E001',
                f'`denormalize_from` is `{self.denormalize_from}`, which does not resolve: {exception}.',
            )

        if chain[0].null:
            return self._error(
                'postgres_denormalize.E002',
                f'`denormalize_from` is `{self.denormalize_from}`, which starts at the nullable foreign key '
                f'`{chain[0].name}`, so the trigger computes NULL for every row where `{chain[0].name}` is empty.',
            )

        return self._check_end_of_chain(chain)

    def _check_end_of_chain(self, chain):
        """
        Check the field at the end of the lookup path. Each concrete class allows different fields there.
        """
        raise NotImplementedError

    def _error(self, check_id, message, hint=None):
        return [checks.Error(message, hint=hint, obj=self, id=check_id)]

    def _warning(self, check_id, message, hint=None):
        return [checks.Warning(message, hint=hint, obj=self, id=check_id)]


class DenormalizedForeignKey(DenormalizedMixin, models.ForeignKey):
    """
    A foreign key that stores a copy of the foreign key at the end of ``denormalize_from``::

        tenant = DenormalizedForeignKey('project__tenant')

    The target model is the model that the last hop points at, so ``to`` is omitted. The field resolves the target once
    every model along the lookup path is loaded, the way Django resolves a lazy ``'app_label.Model'`` reference.
    ``deconstruct`` writes the target into migrations, so a migration names the target like for any other foreign key.
    Passing ``to`` explicitly is allowed as well; the system check then requires it to match the lookup path.

    ``on_delete`` and ``null`` are inferred from the hops in the same walk, when they are omitted. ``on_delete`` follows
    the rules of the hops (see ``infer_on_delete``). The copy can be NULL only when a hop after the first is nullable,
    and then the field is nullable too, because the trigger writes NULL whenever that hop is empty. ``deconstruct``
    writes both into migrations. A declared ``on_delete`` is kept, and the system check warns when it differs from the
    inferred one. A declared ``null`` is kept as well, and the system check requires it on a path that can be NULL.
    """

    def __init__(self, denormalize_from, on_delete=None, *, to=None, **kwargs):
        self.target_from_path = to is None
        self.on_delete_from_path = on_delete is None
        self.null_from_path = 'null' not in kwargs
        self.null_was_inferred = False
        super().__init__(
            INFERRED_TARGET if to is None else to,
            INFERRED_ON_DELETE if on_delete is None else on_delete,
            denormalize_from=denormalize_from,
            **kwargs,
        )

    @property
    def target_is_known(self):
        """
        Whether the target model is known: either it was passed as ``to``, or it is already inferred from the lookup
        path.
        """
        return self.remote_field.model != INFERRED_TARGET

    @property
    def on_delete_is_known(self):
        """
        Whether the ``on_delete`` rule is known: either it was passed, or it is already inferred from the lookup path.
        """
        return self.remote_field.on_delete is not INFERRED_ON_DELETE

    @property
    def null_is_known(self):
        """
        Whether ``null`` is known: either it was passed, or it is already inferred from the lookup path.
        """
        return not self.null_from_path or self.null_was_inferred

    def contribute_to_class(self, cls, name, private_only=False, **kwargs):
        if not self.target_from_path:
            super().contribute_to_class(cls, name, private_only=private_only, **kwargs)
            if (self.on_delete_from_path or self.null_from_path) and not cls._meta.abstract:
                # The target is known, but on_delete or null still has to be inferred from the hops.
                lazy_related_operation(self._walk_to_target, cls, cls, index=0)
            return

        # This does what RelatedField.contribute_to_class and ForeignObject.contribute_to_class do, except that it does
        # not schedule the resolution of `to`. The column has to exist on the model right away, because code that runs
        # while the class is created copies the model's fields (an audit package's decorator, for example). The target
        # is not known until every model along the lookup path is loaded.
        models.Field.contribute_to_class(self, cls, name, private_only=private_only, **kwargs)
        self.opts = cls._meta
        if not cls._meta.abstract:
            self._format_related_names(cls)
            # The first hop is a field of `cls`, which may be declared after this field, so the walk to the target
            # starts once the class is complete and registered.
            lazy_related_operation(self._walk_to_target, cls, cls, index=0)
        setattr(cls, self.name, self.forward_related_accessor_class(self))

    def _format_related_names(self, cls):
        if self.remote_field.related_name:
            related_name = self.remote_field.related_name
        else:
            related_name = self.opts.default_related_name
        if related_name:
            self.remote_field.related_name = related_name % {
                'class': cls.__name__.lower(),
                'model_name': cls._meta.model_name.lower(),
                'app_label': cls._meta.app_label.lower(),
            }
        if self.remote_field.related_query_name:
            self.remote_field.related_query_name = self.remote_field.related_query_name % {
                'class': cls.__name__.lower(),
                'app_label': cls._meta.app_label.lower(),
            }

    def _walk_to_target(self, leaf, owner, index, hops=()):
        """
        Follow the hop at ``index``, which belongs to ``owner``, and schedule the next step once the model that the hop
        points at is loaded. ``hops`` are the hops that the walk has crossed so far.

        Each hop's target may still be a lazy reference, relative to the app of the model that has the hop. Every step
        therefore waits for its own target through a ``lazy_related_operation`` that is scoped to that model. A segment
        that names no hop that a lookup path can cross ends the walk without a target, and the system check reports the
        lookup path.
        """
        segments = self.denormalize_from.split('__')
        try:
            hop = owner._meta.get_field(segments[index])
            if not is_forward_hop(hop):
                return
            refuse_hop(owner, hop, self.denormalize_from, index)
        except FieldDoesNotExist, PathRefused:
            return
        hops = (*hops, hop)
        if index == len(segments) - 1:
            lazy_related_operation(self._complete_from_path, owner, hop.remote_field.model, leaf=leaf, hops=hops)
        else:
            lazy_related_operation(
                self._walk_from_target, owner, hop.remote_field.model, leaf=leaf, index=index + 1, hops=hops
            )

    def _walk_from_target(self, owner, target, leaf, index, hops):
        self._walk_to_target(leaf, target, index, hops)

    def _complete_from_path(self, owner, target, leaf, hops):
        """
        Infer what the declaration omits from ``hops``, the hops of the whole lookup path: the target, ``on_delete`` and
        ``null``.

        The target is ``target``, and the target column is the column that the last hop points at. A last hop that is
        declared with ``to_field`` stores a value of that column, so the copy points at the same column. A field that is
        declared with its own ``to_field`` keeps it, and the system check compares it with the lookup path.

        A hop with a custom ``on_delete`` rule leaves the rule of the copy unknown, and the system check reports it.
        """
        if self.on_delete_from_path:
            rule, _hop = infer_on_delete(hops)
            if rule is not None:
                self.remote_field.on_delete = rule
        if self.null_from_path:
            self.null = any(hop.null for hop in hops[1:])
            self.null_was_inferred = True
        if not self.target_from_path:
            return
        self.remote_field.model = target
        to_field = hops[-1].remote_field.field_name
        if self.remote_field.field_name is None and to_field not in (None, target._meta.pk.name):
            self.remote_field.field_name = to_field
            self.to_fields = [to_field]
        # This does what resolve_related_class in RelatedField.contribute_to_class does once a lazy `to` resolves.
        self.do_related_class(target, leaf)

    def deconstruct(self):
        name, path, args, kwargs = super().deconstruct()
        # Whatever is not known yet is omitted. A copy of the field that is made now, by an audit package that copies
        # fields while the model class is created, infers it from the same lookup path.
        if not self.target_is_known:
            kwargs.pop('to', None)
        if not self.on_delete_is_known:
            kwargs.pop('on_delete', None)
        # ForeignKey.deconstruct omits null=False, because it is the default. A field that is rebuilt without null would
        # infer it again from the lookup path, and in a migration that is the path in the historical state. When a later
        # migration makes a hop nullable, the rebuilt field would then be nullable before the migration that makes the
        # column nullable, and that migration would change nothing. So null is always written once it is known.
        if self.null_is_known:
            kwargs['null'] = self.null
        return name, path, args, kwargs

    def check(self, **kwargs):
        if not self.target_is_known or not self.on_delete_is_known:
            # The target or the rule is unknown only when the lookup path does not lead to a loaded model, or crosses a
            # custom on_delete rule, and the path check reports both. Django's own relation checks would report
            # INFERRED_TARGET as a missing model, or compare INFERRED_ON_DELETE with the rules of the target, which says
            # nothing useful.
            return self._check_denormalize_from()
        return super().check(**kwargs)

    def _check_end_of_chain(self, chain):
        end = chain[-1].related_model._meta.label
        if not self.target_is_known:
            return self._error(
                'postgres_denormalize.E003',
                f'The target of the field is not known, but `{self.denormalize_from}` ends at {end}.',
            )
        if chain[-1].related_model is not self.related_model:
            return self._error(
                'postgres_denormalize.E003',
                f'The field points at {self.related_model._meta.label}, but `{self.denormalize_from}` ends at {end}.',
            )
        if chain[-1].target_field != self.target_field:
            return self._error(
                'postgres_denormalize.E003',
                f'The field points at {end}.{self.target_field.name}, but `{self.denormalize_from}` ends at '
                f'{end}.{chain[-1].target_field.name}. Declare the field with the same to_field as the last hop, or '
                f'without one.',
            )

        nullable = [hop.name for hop in chain[1:] if hop.null]
        if nullable and not self.null:
            return self._error(
                'postgres_denormalize.E004',
                f'`{self.denormalize_from}` crosses the nullable foreign key `{nullable[0]}`, so the trigger writes '
                'NULL for every row where it is empty. Declare the field with null=True, or without null.',
            )

        return self._check_on_delete_rule(chain)

    def _check_on_delete_rule(self, chain):
        """
        Check the ``on_delete`` rule against the rule that the lookup path implies (see ``infer_on_delete``).
        """
        inferred, deciding_hop = infer_on_delete(chain)
        hop_label = f'{deciding_hop.model._meta.label}.{deciding_hop.name}'
        if inferred is None:
            if self.on_delete_is_known:
                # A declared rule cannot be compared with a custom rule along the path.
                return []
            return self._error(
                'postgres_denormalize.E008',
                f'`{self.denormalize_from}` crosses {hop_label}, which has the custom on_delete rule '
                f'{describe_rule(deciding_hop.remote_field.on_delete)}, so the field cannot infer its own rule.',
                hint='Declare on_delete on the field.',
            )
        declared = self.remote_field.on_delete
        if declared is not inferred:
            return self._warning(
                'postgres_denormalize.W009',
                f'The field declares on_delete={describe_rule(declared)}, but `{self.denormalize_from}` implies '
                f'{describe_rule(inferred)}, which follows from the on_delete rule of {hop_label}.',
                hint=f'Declare the field without on_delete, or with on_delete={describe_rule(inferred)}.',
            )
        return []


def copy_column_definition(source, null):
    """
    Return an unbound field with the same class, type, length, precision and collation as the column ``source``.

    The field is nullable when ``null`` is true. It has none of the attributes that describe ``source`` as a form input
    or that add a constraint or index to the table of ``source``.
    """
    _name, _path, args, kwargs = source.deconstruct()
    for omitted in OMITTED_SOURCE_KWARGS:
        kwargs.pop(omitted, None)
    return type(source)(*args, null=null, **kwargs)


class DenormalizedField(DenormalizedMixin, models.Field):
    """
    A column that stores a copy of a column at the end of a chain of foreign keys, such as the name of an ancestor::

        project_name = DenormalizedField('project__name')

    The column has the type of the source column. ``base_field`` is a field with the same type as the source column
    (see ``copy_column_definition``). This field passes everything that depends on the type to ``base_field``: the
    column's database type, collation and check, the conversion of values to and from the database, and the lookups and
    transforms that a query can apply. A query also reads the column as the base field (see ``get_col``). A filter, an
    expression or an index on the copy therefore behaves as it would on the source column.

    ``base_field`` is inferred from the source column the first time it is needed, through the registry of the model
    that declares the field. ``PostgresDenormalizeConfig.ready()`` does that for every model. ``deconstruct`` writes the
    result into the migration, so a migration records the type that the column had when the migration was generated. A
    field that Django rebuilds from migration state uses that recorded type and does not look it up. Changing the
    source column, such as widening it, therefore changes the copy's ``deconstruct`` output, and ``makemigrations``
    alters the copy in the same run.

    The copy is nullable when the source column is nullable, or when a hop after the first is nullable. An empty hop
    leaves no row to read from, and the trigger then writes NULL. ``null`` reads this from ``base_field``.
    """

    copies_column = True

    def __init__(self, denormalize_from, *, base_field=None, **kwargs):
        # The column's nullability always follows from the lookup path, never from the declaration.
        kwargs.pop('null', None)
        self._base_field = base_field
        super().__init__(denormalize_from=denormalize_from, **kwargs)

    @property
    def null(self):
        return self.base_field.null if self.resolve_base_field() else False

    @null.setter
    def null(self, value):
        # Field.__init__ assigns null. The getter derives it from the source column, so the assignment is discarded.
        pass

    @property
    def base_field(self):
        if self._base_field is None and not self.resolve_base_field():
            raise ImproperlyConfigured(
                f'{self} cannot infer its column type, because `{self.denormalize_from}` does not resolve to a '
                f'column. The system checks say why.'
            )
        return self._base_field

    def resolve_base_field(self):
        """
        Infer ``base_field`` from the source column, unless it is already known. Return whether it is known now.

        When the lookup path does not resolve, ``base_field`` stays unknown, so that the system check reports the
        problem and this method does not.
        """
        if self._base_field is not None:
            return True
        model = getattr(self, 'model', None)
        if model is None or not model._meta.apps.models_ready:
            return False
        try:
            chain = self.resolve_chain()
        except FieldDoesNotExist, ValueError:
            return False
        if not _is_copyable_column(chain[-1]):
            return False
        self._base_field = copy_column_definition(chain[-1], null=any(hop.null for hop in chain[1:]))
        return True

    def deconstruct(self):
        name, path, args, kwargs = super().deconstruct()
        kwargs.pop('null', None)
        if self.resolve_base_field():
            kwargs['base_field'] = self._base_field
        return name, path, args, kwargs

    def _check_end_of_chain(self, chain):
        source = chain[-1]
        if source.is_relation:
            return self._error(
                'postgres_denormalize.E005',
                f'`{self.denormalize_from}` ends at the foreign key `{source.name}`. A copy of a foreign key is a '
                f'DenormalizedForeignKey.',
            )
        if source.primary_key:
            return self._error(
                'postgres_denormalize.E005',
                f'`{self.denormalize_from}` ends at the primary key `{source.name}`, whose value the foreign key '
                'before it already stores. A copy of that key is a DenormalizedForeignKey.',
            )
        if isinstance(source, models.GeneratedField):
            return self._error(
                'postgres_denormalize.E005',
                f'`{self.denormalize_from}` ends at the generated column `{source.name}`, which this field does not '
                f'copy.',
            )
        return []

    def get_internal_type(self):
        return self.base_field.get_internal_type()

    def db_type(self, connection):
        return self.base_field.db_type(connection)

    def db_parameters(self, connection):
        return self.base_field.db_parameters(connection)

    def cast_db_type(self, connection):
        return self.base_field.cast_db_type(connection)

    def get_col(self, alias, output_field=None):
        # A query reads the column through a Col whose output field is the base field, the way a ForeignKey's Col
        # reports its target field. Expressions, lookups and value conversion therefore see the class of the source
        # column. Concat and Coalesce resolve their type without a "mixed types" error. Concat skips the cast to text
        # that it applies to anything that is not a CharField or TextField. Lookups that are registered on the class of
        # the source apply as they would to the source column. The Col converts values with the base field alone,
        # because this field has no converter of its own. As a result, Field.cached_col is never used, because Django
        # only returns it for a Col whose output field is the field itself.
        if output_field is None or output_field is self:
            output_field = self.base_field
        return super().get_col(alias, output_field)

    def get_prep_value(self, value):
        return self.base_field.get_prep_value(value)

    def get_db_prep_value(self, value, connection, prepared=False):
        return self.base_field.get_db_prep_value(value, connection, prepared)

    def to_python(self, value):
        return self.base_field.to_python(value)

    def value_to_string(self, obj):
        # The serializers write a value that is not a number, a date or None with value_to_string, and read it back with
        # to_python. Field.value_to_string would write str(value), which to_python of an ArrayField or a JSONField
        # cannot read back, so the base field writes it. The base field reads the value from `obj` through its own
        # attname. It is never added to a model, so it gets the attname of this field first, as ArrayField does for the
        # field of its items.
        base_field = self.base_field
        if getattr(base_field, 'attname', None) != self.attname:
            base_field.set_attributes_from_name(self.name)
        return base_field.value_to_string(obj)

    @property
    def empty_strings_allowed(self):
        return self.base_field.empty_strings_allowed

    def get_lookup(self, lookup_name):
        return self.base_field.get_lookup(lookup_name)

    def get_transform(self, lookup_name):
        return self.base_field.get_transform(lookup_name)

    def formfield(self, **kwargs):
        return None


def _is_copyable_column(field):
    return not (field.is_relation or field.primary_key or isinstance(field, models.GeneratedField))


def resolve_denormalized_fields(apps=global_apps):
    """
    Infer ``base_field`` for every ``DenormalizedField`` in ``apps``, including those on excluded models, because their
    columns need a type too.

    ``PostgresDenormalizeConfig.ready()`` calls this once every model is loaded. Django reads ``null`` when it builds
    queries, so ``null`` has to be known before the first query runs.
    """
    for model in apps.get_models():
        for field in model._meta.concrete_fields:
            if isinstance(field, DenormalizedField):
                field.resolve_base_field()


def get_denormalized_fields(model):
    """
    Return the denormalized fields of both kinds that ``model`` declares, in declaration order.

    Only the fields whose column is in the table of ``model`` count. A model that inherits from a concrete parent, and a
    proxy model, therefore get no triggers, no drift filter and no backfill for the fields of the parent, because the
    triggers of the parent already keep those columns correct.

    For an excluded model (see ``EXCLUDE_MODELS``) this returns no fields, so the model gets no triggers, no drift
    filter and no backfill. The usual reason to exclude a model is an audit table that copies the fields of a tracked
    model, including the denormalized ones. Its rows record the value that a copy had at the time, so no trigger may
    rewrite them.
    """
    if conf.is_excluded(model):
        return []
    return [field for field in model._meta.local_concrete_fields if isinstance(field, DenormalizedMixin)]


def drift_condition(model):
    """
    Build a filter that matches the rows of ``model`` where any denormalized field differs from its lookup path.

    For a model without denormalized fields, such as an excluded model, the filter matches no row. An empty ``Q()``
    would match every row instead.
    """
    fields = get_denormalized_fields(model)
    if not fields:
        return Q(pk__in=[])
    return reduce(operator.or_, (Q(IsDistinctFrom(F(field.attname), F(field.denormalize_from))) for field in fields))


__all__ = [
    'DenormalizedField',
    'DenormalizedForeignKey',
    'DenormalizedMixin',
    'IsDistinctFrom',
    'drift_condition',
    'get_denormalized_fields',
    'infer_on_delete',
    'resolve_denormalized_fields',
]
