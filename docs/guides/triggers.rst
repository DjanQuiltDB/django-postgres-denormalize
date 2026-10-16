===============================
How the copies are kept in sync
===============================

Three kinds of trigger are derived from the field declarations while the app registry starts. They are registered with
django-pgtrigger, and ``makemigrations`` writes them into migrations. Models declare nothing besides the fields.

The examples on this page use :ref:`the example models <example_models>`: ``Task`` copies ``project__tenant``
into ``tenant`` and ``project__name`` into ``project_name``, and ``Comment`` copies ``task__project__tenant`` into
``tenant``.


The set trigger: ``denormalize_set``
------------------------------------

A ``BEFORE INSERT OR UPDATE`` row trigger on the leaf's table computes every denormalized field of the row that is being
written. Because it runs in the database, it covers every write path: the ORM, ``bulk_create``, ``QuerySet.update()``,
raw SQL, ``loaddata``, and the previous release of your application that is still serving during a rolling
deployment and does not know that the column exists.

For ``Task``, the generated function reads:

.. code-block:: plpgsql

    IF TG_OP = 'UPDATE' THEN
        IF OLD."project_id" IS NOT DISTINCT FROM NEW."project_id"
            AND OLD."tenant_id" IS NOT DISTINCT FROM NEW."tenant_id"
            AND OLD."project_name" IS NOT DISTINCT FROM NEW."project_name" THEN
            RETURN NEW;
        END IF;
    END IF;
    SELECT hop0."tenant_id" AS value0, hop0."name" AS value1
    INTO ancestor0
    FROM "tenancy_project" hop0
    WHERE hop0."id" = NEW."project_id";
    IF FOUND THEN
        NEW."tenant_id" := ancestor0.value0;
        NEW."project_name" := ancestor0.value1;
    END IF;
    RETURN NEW;

Each detail is deliberate:

* **One lookup per ancestor row.** All copies that are read from the same row share one ``SELECT``.
* **An early return for ordinary saves.** An UPDATE that changes neither a first hop nor a copy cannot change any copy.
  Django assigns each copy to itself on every save, so for such a save the trigger returns before looking anything
  up. An UPDATE that *writes* a copy, such as a cascade or ``QuerySet.update(project_name=...)``, recomputes the
  copies, so a value that code assigns to a copy is always overwritten.
* **NULL is copied as NULL.** The row is read into a ``record``, and the copies are assigned only ``IF FOUND``. A row
  that is found is copied as it is, including NULL values. No row is found when the transaction inserts a leaf before
  its ancestor. Django creates every foreign key as ``DEFERRABLE INITIALLY DEFERRED``, so any code can do that inside
  a transaction, not only a bulk load such as ``loaddata``. The value that the insert supplies is then kept, and the
  fill trigger on the ancestor's table corrects it when the ancestor is inserted (see below). A copy that is not
  nullable still fails at once, with a NOT NULL error, when the insert supplies no value.
* **Left joins after the first hop.** An empty nullable hop yields NULL, and not an empty result.
* **No column types in DECLARE.** The function declares only generic ``record`` variables, so it can be created before
  the tables that it reads exist. Tools that copy a schema rely on that, because they create the functions first.
* **Quoted names.** Every table and column name is quoted the way Django quotes it, so a ``db_table`` or ``db_column``
  with uppercase letters or a reserved word works. The aliases that the trigger makes up, such as ``hop0``, are not
  quoted.


The cascade trigger: ``denormalize_cascade_<app>_<leaf>``
---------------------------------------------------------

A change to an ancestor, such as a move to another tenant or a rename, writes nothing to the leaf's table, so the set
trigger never runs for the leaf rows. An ``AFTER UPDATE`` row trigger on each owner's table rewrites them. The trigger
is on the owner's table, but it is named after the leaf that it serves. For the example models, the trigger on
``tenancy_project`` that rewrites tasks is therefore ``denormalize_cascade_tenancy_task``:

.. code-block:: postgresql

    -- on tenancy_project, WHEN ((OLD."tenant_id" IS DISTINCT FROM NEW."tenant_id"
    --     OR OLD."name" IS DISTINCT FROM NEW."name")
    --     AND NOT COALESCE('pgtrigger_denormalize_cascade_tenancy_task_de430' = ANY(string_to_array(
    --         current_setting('postgres_denormalize.deferred_cascades', true), ',')), false))
    UPDATE "tenancy_task" leaf
    SET "tenant_id" = NEW."tenant_id",
        "project_name" = NEW."name"
    WHERE leaf."project_id" = NEW."id"
      AND (leaf."tenant_id" IS DISTINCT FROM NEW."tenant_id"
        OR leaf."project_name" IS DISTINCT FROM NEW."name");

``de430`` is a hash that pgtrigger appends to the trigger's name.

* **One trigger per leaf and owner**, which rewrites all of that leaf's copies in one statement. An ancestor that is
  renamed and moved at once writes each leaf row once.
* **A WHEN condition on the copied columns.** Every other UPDATE of the owner, such as of a counter or a timestamp,
  never calls the function. The second half of the condition lets :func:`~postgres_denormalize.batch.defer_cascades`
  skip the trigger for a batch (see :doc:`/guides/batch_updates`). With ``DEFERRABLE_CASCADES = False``, the condition
  does not have that second half.
* **Only rows that change are written.** A leaf row whose copies would not change is not written. Moving a row to
  another parent of the same tenant, for example, does not touch the rows below it: no write, no lock, no history row.
* **Deep paths are walked.** A change two hops above the leaf rewrites the leaves through the tables in between.
* **Row-level, on purpose.** A statement-level trigger with transition tables could rewrite the leaves of many moved
  rows in one set-based UPDATE. But the ``WHEN`` condition of a statement-level trigger cannot refer to column values,
  and PostgreSQL does not combine transition tables with an ``UPDATE OF column`` list. Such a trigger would therefore
  fire, and build transition tables, for every UPDATE of the owner's table. With the row-level design, single-row edits
  are cheap, and the cost of bulk reparenting is proportional to the number of moved rows. See
  :doc:`/getting_started/when_to_use`, or wrap a large batch in a batch helper (:doc:`/guides/batch_updates`).

Rows that a soft-delete library has only marked as deleted are rewritten too, so restoring one never brings back an
out-of-date copy.


The fill trigger: ``denormalize_fill_<app>_<leaf>``
---------------------------------------------------

Django checks foreign keys only when the transaction commits, so a transaction can insert a leaf row before the
ancestor row that its lookup path reads. The set trigger then finds no ancestor row and keeps the value that the insert
supplies. An ``AFTER INSERT`` row trigger on each owner's table fills those copies when the ancestor row is inserted.
For the example models, the trigger on ``tenancy_project`` that fills tasks is ``denormalize_fill_tenancy_task``. Its
function runs the same statement as the cascade trigger:

.. code-block:: postgresql

    -- on tenancy_project, AFTER INSERT, without a WHEN condition
    UPDATE "tenancy_task" leaf
    SET "tenant_id" = NEW."tenant_id",
        "project_name" = NEW."name"
    WHERE leaf."project_id" = NEW."id"
      AND (leaf."tenant_id" IS DISTINCT FROM NEW."tenant_id"
        OR leaf."project_name" IS DISTINCT FROM NEW."name");

When the rows are inserted in the usual order, the new ancestor row has no leaf rows yet, so each insert costs one
indexed lookup that writes nothing. The batch helpers do not skip the fill triggers.


Triggers on a model of an installed package
-------------------------------------------

The cascade and fill triggers are registered on the model that the hop before the column points at. When a lookup path
reads a column of a model from an installed package, such as the email of ``auth.User``, pgtrigger adds the triggers to
the migration state of that package. ``makemigrations`` then writes a migration into the package's directory in
site-packages, or reports a change that never goes away. System check ``postgres_denormalize.W302`` reports this.

Point the foreign key at a proxy model in one of your own apps instead. The triggers are then registered on the proxy
model, and their migration belongs to your app. The proxy model uses the same table, so an update through the
package's own model still runs them:

.. code-block:: python

    from django.contrib.auth.models import User


    class Reviewer(User):
        class Meta:
            proxy = True


    class Review(models.Model):
        reviewer = models.ForeignKey(Reviewer, on_delete=models.CASCADE)
        reviewer_email = DenormalizedField('reviewer__email')

The batch helpers accept either the proxy model or the package's model as the owner.


.. _concurrent_writes:

Concurrent writes
-----------------

The set trigger reads the ancestor row with a plain ``SELECT``, and the cascade trigger rewrites the leaf rows that
its ``UPDATE`` can see. At ``READ COMMITTED``, which is Django's default, two transactions that run at the same time
can therefore commit a copy that differs from its lookup path. With the example models:

1. Transaction A moves a project to another tenant. Its cascade trigger rewrites the tasks that exist, and A has not
   committed yet.
2. Transaction B inserts a task for that project. The set trigger does not wait for A, so it reads the tenant that was
   last committed, which is the old one.
3. Both commit. The cascade of A never saw the task of B, so that task keeps the old tenant.

``REPEATABLE READ`` does not prevent this either. ``SERIALIZABLE`` does: PostgreSQL detects that the two transactions
depend on each other and fails one of them with a serialization error, which the application retries. Locking the
ancestor rows in the set trigger would also prevent it, but the set trigger locks the rows from the leaf up, and a
cascade writes them from the ancestor down. Two such transactions would then wait for each other and fail with a
deadlock instead.

Run the transactions that write leaves or owners at ``SERIALIZABLE``, and retry those that fail:

.. code-block:: python

    from django.db.backends.postgresql.psycopg_any import IsolationLevel

    DATABASES = {
        'default': {
            # ...
            'OPTIONS': {'isolation_level': IsolationLevel.SERIALIZABLE},
        },
    }

System check ``postgres_denormalize.W303`` reports every database that stores copies and runs at a lower level. If a
lower level is a deliberate choice, set ``ISOLATION_CHECK_LEVEL`` to ``'off'`` (see :doc:`/reference/settings`), and
find and correct such copies periodically with ``drift_condition`` and ``backfill_denormalized`` (see
:doc:`/guides/drift`).
