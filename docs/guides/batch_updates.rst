=============
Batch updates
=============

A statement that changes copied columns on many owner rows runs the cascade trigger once per row (see
:doc:`/guides/triggers`). For a batch job, wrap the statements in one of two context managers. Inside the block, the
cascade triggers on the named owners are skipped. When the block ends, every leaf that those triggers serve is
corrected with :func:`~postgres_denormalize.operations.backfill_denormalized`, with one set-based UPDATE per
denormalized field. With :ref:`the example models <example_models>`:

.. code-block:: python

    from postgres_denormalize.batch import defer_cascades

    with defer_cascades(Project):
        Project.objects.filter(tenant=old).update(tenant=new)
        Project.objects.filter(pk__in=retired).update(name='Archive')

Name the models whose rows the batch updates, which are the owners, and not the leaves that copy from them. The block
and the correction run in one transaction (or in a savepoint, if a transaction is already open). Other transactions
therefore never see out-of-date copies, and an exception rolls the whole batch back.

.. warning::

    **Inside the block, the copies below the changed rows are out of date.** The block's own transaction sees them with
    their old values until the block ends. Code in the block that reads a copy therefore gets a wrong answer, and
    nothing reports it:

    .. code-block:: python

        with defer_cascades(Project):
            Project.objects.filter(tenant=old).update(tenant=new)

            Task.objects.filter(tenant=new)  # does not find the tasks of the moved projects
            Task.objects.filter(tenant=old)  # still finds them

    * A query or a permission check that is scoped to one tenant, or to any other copied value, puts the rows below a
      changed row under the old value.
    * Saving a leaf row in the block does not correct it. Django writes the copy back unchanged, so the set trigger
      skips the row.
    * Code that the block runs without naming it reads the same old values: signal receivers, ``save()`` methods and
      service functions.
    * A unique constraint on a copy fails only when the block ends (see :doc:`/guides/indexing`).

    Put only the batch's own writes to the owners in the block. Read the copies after the block ends. If code in the
    block needs the value, follow the lookup path, such as ``project__tenant``, and not the copy. Do not wrap code that
    you do not control.


Choosing a helper
-----------------

.. list-table::
    :header-rows: 1
    :widths: 20 40 40

    * -
      - ``ignore_cascades``
      - ``defer_cascades``
    * - How
      - ``pgtrigger.ignore()``
      - A transaction-local Postgres setting that the trigger's ``WHEN`` condition reads
    * - Per changed owner row
      - The trigger function is called and returns at once
      - Nothing; Postgres does not call the function
    * - Skipped for
      - Queries that Django sends from the current thread
      - Every statement in the transaction, raw SQL included
    * - Needs
      - Nothing
      - ``DEFERRABLE_CASCADES`` (on by default); see :ref:`deferrable_cascades`


.. _batch_update_cost:

What it costs
-------------

Neither helper makes the leaf's side cheaper. The correction reads every row of each affected leaf to find the rows that
differ, and each row that it rewrites runs the leaf's ``denormalize_set`` trigger, because the UPDATE writes a copy.
Both save the per-row work on the owner's side. That work dominates when a batch changes many owner rows that each
have few leaf rows below them. For a handful of rows, the plain cascade is cheaper than a scan of the leaf.
