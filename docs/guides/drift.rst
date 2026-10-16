===============================
Inspecting and repairing copies
===============================

The triggers keep every copy equal to its source for every write that PostgreSQL sees. A write that the triggers do not
see can leave copies out of date: a ``DISABLE TRIGGER``, a dump that is restored without its triggers, or a replication
setup that skips triggers. This page describes how to find such rows and how to correct them. The examples use
:ref:`the example models <example_models>`.


Finding stale copies
--------------------

:func:`~postgres_denormalize.fields.drift_condition` builds a filter for the rows whose copies differ from their
paths:

.. code-block:: python

    from postgres_denormalize.fields import drift_condition

    Task.objects.filter(drift_condition(Task)).exists()

It uses ``IS DISTINCT FROM``, so NULLs compare correctly. For an excluded model, such as an audit table, it matches no
row, because this package does not keep its copies in sync.

The filter is an ordinary ``Q`` object, so it combines with any queryset. To see how many rows are stale, and which
ones, before you change anything:

.. code-block:: python

    stale = Task.objects.filter(drift_condition(Task))
    stale.count()
    stale.values('pk', 'tenant', 'project__tenant', 'project_name', 'project__name')[:20]


Correcting them
---------------

:func:`~postgres_denormalize.operations.backfill_denormalized` sets every copy of a model to the value at the end of
its path, with one set-based ``UPDATE`` per field. Rows that already have the right value are not written. It runs no
transaction of its own, so wrap the calls in one:

.. code-block:: python

    from django.db import transaction
    from postgres_denormalize.operations import backfill_denormalized

    with transaction.atomic():
        backfill_denormalized(Task)
        backfill_denormalized(Comment)

Call it once for each leaf. The correction of one leaf does not correct the others: a lookup path never reads another
copy (see :doc:`/guides/fields`), so correcting a copy changes no column that a cascade trigger watches. For the same
reason, the order of the calls does not matter.

After a restore or a period with disabled triggers, any leaf can be stale. This corrects every leaf in the project:

.. code-block:: python

    from django.apps import apps
    from django.db import transaction
    from postgres_denormalize.fields import get_denormalized_fields
    from postgres_denormalize.operations import backfill_denormalized

    with transaction.atomic():
        for model in apps.get_models():
            if get_denormalized_fields(model):
                backfill_denormalized(model)

Before you run it on a large table, note the following:

* **Database.** ``using`` names the database to correct. Without it, the call writes to the database that the router
  chooses for the model. A project with several databases calls it once for each database.
* **Cost.** Each call reads every row of the leaf to find the rows that differ. Each row that it rewrites runs the
  leaf's set trigger and, if a history package tracks the leaf, gets a history row.
* **Unique constraints.** When a copy is part of a unique constraint, the correction can make two rows collide, and the
  PostgreSQL error names only the first collision (see :ref:`collisions <migration_collisions>`).
* **Excluded models.** The call skips a model that ``EXCLUDE_MODELS`` lists, such as an audit table, because its rows
  record past values. ``also_fill`` writes the copies of such a model, but it overwrites every row with the current
  value. It is meant for filling a new column, not for repairs (see :ref:`copies on excluded models
  <excluded_model_copies>`).


In a migration
--------------

The :class:`~postgres_denormalize.operations.BackfillDenormalized` migration operation calls
``backfill_denormalized`` on the database of the migration. A migration needs it when it adds a denormalized field or
turns an existing column into one:

.. code-block:: python

    from postgres_denormalize.operations import BackfillDenormalized

    operations = [
        ...,
        BackfillDenormalized('task'),
    ]

:doc:`/guides/migrations` describes where it goes in the migration.
