================
Excluding models
================

Some models have a denormalized field that must not be kept in sync. Two keys of ``POSTGRES_DENORMALIZE`` name such
models: ``EXCLUDE_MODELS`` lists them by label, and ``EXCLUDE_MODELS_CALLBACK`` recognizes them by a rule, such as
every model that a package generates. A model is excluded when either key says so. See :doc:`/reference/settings` for
the values that each key takes.

This package skips an excluded model entirely:

* it gets no triggers, so nothing ever rewrites its copies;
* :func:`~postgres_denormalize.fields.get_denormalized_fields` returns nothing for it, so the drift filter and the
  backfill skip it (unless ``also_fill`` names it explicitly, see :doc:`/guides/migrations`);
* the project-wide system checks do not look at it;
* its fields skip the field checks that compare the declaration with the lookup path, such as ``W009`` for an
  ``on_delete`` rule that differs from the path. ``EXCLUDE_MODELS_SILENCED_CHECKS`` chooses which ones (see
  :doc:`/reference/settings`).

Its denormalized columns still get their type, so the table is created and migrated like any other. The type, and the
target of a foreign key copy, come from the lookup path, so the path must resolve on the excluded model too. The field
checks that report a path that does not resolve therefore still run by default.

Without either key, no model is excluded. An ``EXCLUDE_MODELS`` entry that names no installed model is reported
(``E401``, see :doc:`/reference/checks`).


Example: audit and history tables
---------------------------------

The usual reason to exclude a model is an audit or history table. Packages that record the past states of a row
usually generate a second model for each tracked model, which copies its fields, including the denormalized fields and
their field classes. Without an exclusion, this package would treat that table as one more leaf. The table would get a
set trigger, and every cascade would rewrite past rows whenever an ancestor moves or is renamed. The history would then
not show the value that the row had at the time.

A few audit tables can simply be listed:

.. code-block:: python

    POSTGRES_DENORMALIZE = {'EXCLUDE_MODELS': ['tenancy.TaskAudit', 'tenancy.CommentAudit']}

When a package generates an audit model for every tracked model, a rule also covers the audit models that are added
later. For example, say that every audit model has the name of its model followed by ``Audit``, and has an
``audit_id`` primary key:

.. code-block:: python

    # myproject/audit.py
    def is_audit_model(model):
        return model._meta.model_name.endswith('audit') and any(
            field.name == 'audit_id' for field in model._meta.fields
        )

.. code-block:: python

    POSTGRES_DENORMALIZE = {'EXCLUDE_MODELS_CALLBACK': 'myproject.audit.is_audit_model'}

.. warning::

    If your project uses an audit or history package that copies model fields, exclude its models. Without that, the
    history tables get cascade triggers that rewrite their rows.


What it looks like in the audit log
-----------------------------------

A *tracked* leaf, which is the model that the audit table records, still gets an audit row each time a cascade
rewrites one of its copies, because the row did change. Renaming an ancestor with a thousand leaf rows below it writes
a thousand audit rows. If that is too much noise for your audit log, it is a reason to copy fewer columns, or columns
that change less often; see :doc:`/getting_started/when_to_use`. The cascade writes only rows whose copies actually
change, so updates that do not change the copied columns write no audit rows at all.


Writing the callback
--------------------

The callback is also called with models that Django rebuilds from migration state, because django-pgtrigger compiles
trigger SQL against them during ``makemigrations``. Such a model has the fields, ``Meta`` options and label of the real
model, but it has no Python base classes except ``models.Model``, so an ``issubclass`` test against an abstract base
class fails for it. Recognize excluded models by something that the migration state also has, such as a name, an app
label, or a column that every excluded model has. (``EXCLUDE_MODELS`` does not have this problem, because the
migration state has the labels.)

Keep the callback cheap and free of side effects. It is called for every model whenever the triggers are derived and
whenever the checks run.
