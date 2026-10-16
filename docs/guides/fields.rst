======
Fields
======

Both fields take the lookup path as their first argument, ``denormalize_from``, written like an ORM lookup. Each field
checks its own declaration when ``manage.py check`` runs. This page names those checks in brackets, and
:doc:`/reference/checks` lists them all. The examples on this page use :ref:`the example models <example_models>`.


Lookup paths
------------

Every segment of the path except the last is a hop, and each hop leads to exactly one row:

* **A hop is a forward foreign key or one-to-one field** of the model that it is resolved against. A reverse relation
  or a many-to-many leads to many rows and is refused (``E001``). A foreign key with ``to_field`` is a hop like any
  other: the triggers follow it by the column that it points at.
* **A hop is declared on the model itself.** A model that inherits from a concrete parent has the parent's fields, but
  their columns are in the parent's table. The path walks through the parent link (``E007``). If ``Project``
  inherited ``tenant`` from a concrete model ``ProjectBase``, the path would be ``project__projectbase_ptr__tenant``,
  and not ``project__tenant``.
* **A path never crosses or ends at another denormalized field** (``E006``). It copies from the source directly.
  ``Task.tenant`` is itself a copy of ``project__tenant``, so ``Comment`` copies ``task__project__tenant``, and not
  ``task__tenant``. A copy of a copy would depend on the order in which the triggers run.

The field belongs to the model that declares it. A model that inherits from a concrete parent, and a proxy model, get
no triggers for the fields of the parent, because the parent's triggers already keep those columns correct.


DenormalizedForeignKey
----------------------

Copies the foreign key at the end of a chain of foreign keys:

.. code-block:: python

    class Comment(models.Model):
        task = models.ForeignKey(Task, on_delete=models.CASCADE)
        tenant = DenormalizedForeignKey('task__project__tenant')

It is a real ``ForeignKey``: it has a database constraint and a reverse accessor (``related_name`` works as usual), and
it can be part of a ``UniqueConstraint`` and of indexes.

**The target is inferred.** ``to`` is omitted, because the last hop already names the target model. The field infers
the target once every model along the path is loaded, in the same way as Django resolves a lazy ``'app_label.Model'``
reference. The models along the path may therefore be declared in any order, in any app, and by string references.
``makemigrations`` writes the target into the migration as ``to=...``, like for any foreign key, so a migration never
depends on the inference.

When the last hop is declared with ``to_field``, the copy points at the same column, and ``makemigrations`` writes that
``to_field`` into the migration as well.

Passing ``to`` or ``to_field`` explicitly is allowed, as long as it matches the path (``E003``).

**The on_delete rule is inferred.** When the row at the end of the path is deleted, the hops decide what happens to a
leaf row, so ``on_delete`` is omitted too. The field starts at the hop next to the deleted row and walks toward the
leaf. A hop that cascades deletes its own row, so the next hop decides. The first hop that does not cascade decides the
rule of the copy:

================================================ ==========================================
Rule of the deciding hop                         Rule of the copy
================================================ ==========================================
``CASCADE`` on every hop                         ``CASCADE``
``SET_NULL``                                     ``SET_NULL``
``PROTECT``                                      ``PROTECT``
``RESTRICT``                                     ``RESTRICT``
``SET(...)``, ``SET_DEFAULT``, ``DO_NOTHING``    ``DO_NOTHING``: the leaf row stays, and the
                                                 trigger rewrites the copy
================================================ ==========================================

On Django 6.1, the database rules map in the same way: ``DB_CASCADE`` to ``DB_CASCADE``, ``DB_SET_NULL`` to
``DB_SET_NULL``, and ``DB_SET_DEFAULT`` to ``DO_NOTHING``. The copy therefore deletes, refuses or sets to NULL exactly
the rows that the path would, and never more. A hop with a custom rule says nothing about the copy, so the field then
needs an explicit ``on_delete`` (``E008``).

Passing ``on_delete`` explicitly is allowed as well. When it differs from the rule that the path implies, the check
warns (``W009``): a ``CASCADE`` on a path that sets the copy to NULL, for example, deletes leaf rows that the path
keeps. Add ``postgres_denormalize.W009`` to ``SILENCED_SYSTEM_CHECKS`` to accept the difference.

**Nullability is inferred.** If any hop after the first is nullable, the copy is NULL whenever that hop is empty, so the
field is nullable. Otherwise it is not. A declaration with ``null=False`` on such a path is an error (``E004``), and
``null=True`` on any path is kept. The first hop itself must not be nullable (``E002``): a leaf without its first hop
has nothing to copy from.

``makemigrations`` writes the inferred rule and nullability into the migration, like the target. A declaration that
omits them therefore writes the same migration as one that passes the inferred values. Removing a declared value from
an existing field changes the migration only when that value differs from the inferred one.


DenormalizedField
-----------------

Copies any other column, such as a name, a code or a date:

.. code-block:: python

    class Task(models.Model):
        project = models.ForeignKey(Project, on_delete=models.CASCADE)
        project_name = DenormalizedField('project__name')

**The column type is inferred.** The field builds a *base field* with the same class, length, precision and collation
as the source column, but with none of its constraints, indexes, defaults or form attributes. The field passes
everything that depends on the type to that base field: the database type, the conversion of values, lookups and
transforms. Queries read the column as the base field, so ``Concat``, ``Coalesce``, ``icontains``, ordering and
expression indexes treat the copy exactly as they treat the source column.

``makemigrations`` records the base field in the migration:

.. code-block:: python

    postgres_denormalize.fields.DenormalizedField(
        base_field=models.CharField(max_length=100),
        denormalize_from='project__name',
    )

Changing the source column, such as widening a ``CharField``, therefore also changes the copy's migration state, and
``makemigrations`` alters both in the same run.

**Nullability** follows the path: the copy is nullable when the source column is nullable, or when a hop after the
first is nullable. The first hop itself must not be nullable (``E002``).

A ``DenormalizedField`` cannot end at a foreign key (that is a ``DenormalizedForeignKey``), at a primary key (the
foreign key before it already stores that value) or at a generated column (``E005``).


Rules for application code
--------------------------

**Never assign a denormalized field to change it.**
    The trigger overwrites any value that the code supplies, on insert and on update. Both fields are non-editable and
    blank, so no ``ModelForm`` or admin form offers them as inputs, and ``full_clean()`` never asks for them. List them
    in an admin's ``readonly_fields`` to show them. Passing the current value is harmless.

**The instance is current after save().**
    On an insert, Django reads the computed value back through ``RETURNING``. On an update, the field gives Django an
    expression, so the computed value comes back in the same way. Two kinds of write do not update the instance and
    need ``refresh_from_db()``: a ``save(update_fields=...)`` that does not list the field, and ``QuerySet.update()``.

**Read through the copy.**
    Filter, order and scope by the copy (``project_name``, ``tenant``), and not by the path that it copies
    (``project__name``, ``project__tenant``). The indexes cover the copy, and walking the path pays for the joins that
    the copy is meant to avoid. A ``Meta.ordering`` that walks a copied path is reported (``W201``).


Foreign keys that are already copies
------------------------------------

A plain ``ForeignKey`` whose target the model can also reach through another chain of foreign keys stores a copy,
whether or not anyone calls it one, and nothing keeps it equal to the other route: only a trigger could, and a plain
foreign key has none. The redundant copy check reports such a foreign key (``W101``), and its hint names the other
path:

.. code-block:: python

    # Reported: Comment can also reach Tenant through task__project__tenant.
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE)

    # Declared as a copy.
    tenant = DenormalizedForeignKey('task__project__tenant')

:doc:`/guides/migrations` describes how to convert an existing column. When the two routes may lead to different rows
on purpose, such as a tenant that is chosen independently, list the field in ``REDUNDANT_COPY_CHECK['ALLOWED']`` with
the reason (see :doc:`/reference/settings`):

.. code-block:: python

    # settings.py
    POSTGRES_DENORMALIZE = {
        'REDUNDANT_COPY_CHECK': {
            'ALLOWED': {
                'tenancy.Task.billed_tenant': 'A task can be billed to a tenant that does not own its project.',
            },
        },
    }

The check is a heuristic. It walks the hops that a lookup path may cross breadth-first, up to ``MAX_DEPTH`` hops, so
the path in its hint is always one that a ``DenormalizedForeignKey`` accepts. A self-referential tree reaches its own
model again through the parent link, so the check can report a field that is not a copy. ``ALLOWED`` is where such
cases are explained. A field that the model inherits from a concrete parent is checked on the parent.
