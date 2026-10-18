============
Alternatives
============

Several tools keep a derived value in a column. They make different trade-offs, and for many applications one of them
is a better fit than this package. This page compares them based on their own documentation. Where a tool's
documentation does not say how something works, this page says so and does not guess.


At a glance
-----------

.. list-table::
    :header-rows: 1
    :widths: 22 26 26 26

    * -
      - django-postgres-denormalize
      - django-pgconstraints ``GeneratedFieldTrigger``
      - django-computedfields
    * - What the column stores
      - A copy of one column or foreign key at the end of a chain of foreign keys
      - The result of an expression over the row and over columns along foreign-key chains
      - The return value of a Python method, which may depend on related rows, reverse relations and many-to-many
    * - Kept in sync by
      - PostgreSQL triggers
      - PostgreSQL triggers
      - Python, after ORM saves
    * - Write paths covered
      - Every write PostgreSQL sees, raw SQL and older releases included
      - Every write PostgreSQL sees
      - ORM saves. ``QuerySet.update()``, bulk related-manager actions and fixtures need ``update_dependent`` or the
        ``updatedata`` command; raw SQL is not seen
    * - Column type
      - Inferred from the source column and recorded in migrations
      - Declared by you
      - Declared by you
    * - Databases
      - PostgreSQL only
      - PostgreSQL only
      - Not tied to PostgreSQL
    * - Filling existing rows
      - A migration operation, which a router can run on each database
      - A management command
      - A management command
    * - Index and constrain the value
      - Yes, it is an ordinary column
      - Yes, the target is an ordinary column
      - Yes, the computed field is an ordinary column


django-pgconstraints
--------------------

`django-pgconstraints <https://github.com/oliverhaas/django-pgconstraints>`_ is the closest relative: like this
package it compiles to PL/pgSQL triggers through django-pgtrigger. It offers three classes, declared in
``Meta.triggers``.

``GeneratedFieldTrigger`` keeps a column that you declare equal to an expression, which may reach through foreign keys
of any depth (``F('part__supplier__markup_pct')``). A forward trigger recomputes the column on each write. Reverse
triggers on the related models recompute the related rows when those models change. ``save()`` and ``bulk_create()``
read the computed value back. A ``refresh_computed_field`` command fills existing rows.

``UniqueConstraintTrigger`` and ``CheckConstraintTrigger`` enforce uniqueness and checks across foreign keys without
storing anything. According to its documentation, uniqueness is checked with an advisory lock and an ``EXISTS`` scan,
and a real unique index (``index=True``) is not available when the fields traverse foreign keys.

Its documentation does not say whether the reverse triggers are row- or statement-level, whether they skip related
rows whose value would not change, or how a NULL along the path is handled.

**Choose django-pgconstraints** when the value is a *computation* (``price * quantity``, a markup over a foreign-key
chain) rather than a copy, or when you need cross-table check constraints, or uniqueness on values you do not want to
store.

**Choose this package** when the value is a straight copy of an ancestor's column or key and you want:

* the column type, and for a foreign key the target model, inferred from the path and kept up to date through
  migrations, instead of declared by hand;
* a copied foreign key that is a real foreign key, with its constraint and reverse accessor;
* documented behavior for NULLs and missing rows, and cascades that write only the rows whose copies change, so that
  unrelated updates create no writes, locks or history rows;
* the backfill as a migration operation, and system checks that find foreign keys that are copies but are not declared
  as copies.


django-computedfields
---------------------

`django-computedfields <https://github.com/netzkolchose/django-computedfields>`_ stores the return value of a Python
method in a database field. It recomputes the value in Python when the row, or a row that it declares as a dependency,
is saved. Dependencies may follow foreign keys, reverse relations and many-to-many relations.

Because it works in Python, the value can be anything that Python can compute, and it is not tied to PostgreSQL. The
downside, according to its own documentation, is that its update model "does not work with bulk actions".
``QuerySet.update()`` and bulk related-manager actions need a call to ``update_dependent``, fixtures need the
``updatedata`` command, and it does not see writes from raw SQL or from another application that shares the database.
It also warns that deep or one-to-many dependency chains and many-to-many dependencies get expensive.

**Choose django-computedfields** when you are not on PostgreSQL, when the value is real Python logic or an aggregate
over reverse or many-to-many relations, and when every write goes through the ORM.

**Choose this package** when the value is a copy that must be correct after bulk updates, raw SQL, data migrations
and writes from the previous release during a rolling deploy.


Not denormalizing at all
------------------------

``select_related`` / ``annotate``
    For displaying an ancestor's value, a join over a primary key is cheap and always correct. Prefer it unless an
    index, a constraint, or sorting and filtering across several joins needs the value; see
    :doc:`/getting_started/when_to_use`.

Django's ``GeneratedField``
    A stored generated column is computed by PostgreSQL itself, with no triggers at all, but only from the row's own
    columns. It cannot read another table.

Materialized views
    A materialized view can precompute any join and can be indexed. But it is a separate relation that is refreshed on
    demand or on a schedule, so it lags behind writes, and it cannot have the leaf's own constraints.

Hand-written triggers
    This package generates the triggers that you would otherwise write and maintain by hand, and regenerates them when a
    path or a column changes. Writing them yourself gives full control, but then you have to update every trigger for
    every model change.
