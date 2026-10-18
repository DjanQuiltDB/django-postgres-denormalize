==========
Migrations
==========

This page is about adding a copy to a model that already has rows, and about deploying it.


Adding a denormalized field
---------------------------

1. Declare the field.

2. Run ``makemigrations``. It writes the column, the changed set trigger on the leaf and the changed cascade and fill
   triggers on every owner. If the leaf is tracked by a history package whose triggers list every column, those
   triggers are regenerated as well.

   For a new ``DenormalizedField`` of a string type, Django fills existing rows with ``''`` while it adds the column.
   For other types, it asks for a one-off default. Any value of the type will do, because the next step replaces it.

3. Add :class:`~postgres_denormalize.operations.BackfillDenormalized` to the migration, **before** the operation that
   installs the set trigger. For the ``project_name`` copy on ``Task`` from :ref:`the example models
   <example_models>`:

   .. code-block:: python

       from postgres_denormalize.operations import BackfillDenormalized

       operations = [
           migrations.AddField('task', 'project_name', ...),
           BackfillDenormalized('task'),
           pgtrigger.migrations.AddTrigger(model_name='task', trigger=...),
       ]

   It fills the new column on every row, and corrects any existing copy that differs from its path, with one set-based
   ``UPDATE`` per field. Rows that already have the right value are not written. In a project with several databases,
   it runs on the databases that the routers allow for the model, as the ``AddField`` does: it passes the model's name
   to ``allow_migrate`` as the ``model_name`` hint. A model that is written as ``'app_label.model_name'`` gives no
   ``model_name`` hint, because ``allow_migrate`` would pair it with the migration's own app label.

   A ``DenormalizedForeignKey`` always writes ``null`` into the migration, also when it is ``False``. A later migration
   that makes a hop nullable is then followed by an ``AlterField`` that makes the copy's column nullable.

4. Read through the new field: point ``Meta.ordering``, filters and indexes at it.


Converting an existing column
-----------------------------

A plain ``ForeignKey`` that is meant to be equal to its path, such as a tenant foreign key that application code keeps
equal to the parent's tenant, can become a ``DenormalizedForeignKey`` in place. The ``AlterField`` changes only the
field class, and the backfill corrects every row whose value differs from its path. When the old foreign key has an
``on_delete`` rule or a nullability that differs from the values that the path implies, and the new declaration omits
them, the ``AlterField`` changes those as well (see :doc:`/guides/fields`).

.. _migration_collisions:

**Collisions.** If the column is part of a unique constraint, correcting rows can make two of them collide, and the
PostgreSQL error names only the first collision. Put a ``RunPython`` before the backfill that lists every row that the
correction would make collide, and that raises an error with all of them.


Rolling deployments
-------------------

PostgreSQL runs a migration in one transaction, and ``AddField`` keeps an exclusive lock on the table until the
transaction commits, so no other session sees a half-filled column. After the commit, the set trigger fills the column
on every insert and update, including the inserts and updates of the previous release, which never sets the column
itself. A NOT NULL denormalized column therefore needs no ``db_default`` for the previous release.


.. _excluded_model_copies:

Copies on excluded models
-------------------------

An excluded model that copies the leaf's columns, such as an audit table (see :doc:`/guides/exclusions`), also gets a
new denormalized column when the leaf gets one. The column is filled with the ``''`` or one-off default from step 2.
``also_fill`` gives those rows the current value at the end of their own lookup path. For an audit table, that is the
closest record of the past:

.. code-block:: python

    BackfillDenormalized('task', also_fill={'taskaudit': ['project_name']})

Model names are resolved in the migration's own app, unless they are written as ``'app_label.model_name'``. A row whose
first hop points at a row that does not exist anymore keeps its value.
