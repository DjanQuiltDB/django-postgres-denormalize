===============================
When to use it, and when not to
===============================

A denormalized column trades write cost for read capability. Every copy adds work to the writes that create, move and
rename rows, and in exchange the database can index, sort, filter and constrain on a value without joining for it. That
trade is worth making for some columns and clearly not for others. For the tools that make a different trade, see
:doc:`/getting_started/alternatives`.


Good reasons
------------

**An index needs the value.**
    This is the strongest reason, because nothing else makes it possible: a PostgreSQL index only reads the indexed
    table. A composite index that starts with copied columns, such as a tenant and a parent's name, can serve a list
    that is scoped to one tenant and sorted by that name straight from the index, already in order, with no join and no
    sort. Trigram (``gin_trgm_ops``) and full-text indexes over an
    ancestor's name work the same way. See :doc:`/guides/indexing`.

**A unique constraint needs the value.**
    "Unique within a tenant" is a ``UniqueConstraint(fields=['tenant', 'title'])`` once the tenant is a column of the
    table. The alternative, a trigger that checks with a ``SELECT``, needs locking to be free of race conditions and
    cannot use an index for the check.

**Sorting and filtering on deep relations.**
    When every list view orders or filters by a value three or four tables away, each of those queries pays for the
    joins, and the planner has fewer good options. A copy makes them single-table queries.

**Tenant scoping.**
    Scoping every query by account or tenant is the classic case: the copy is read on every request, and the tenant of
    a row rarely changes.


Reasons not to
--------------

**Do not use it to save a join for rendering.**
    If all you want is an ancestor's value in a template, use ``select_related()``. A join over a primary key is cheap,
    it is always correct, and it costs nothing on writes. A copy that is only used for rendering adds a column, three
    triggers and extra writes on every rename. If the table is tracked by a history package, it also adds a history row
    for every copy that is rewritten. All of that avoids something that the database already does well.

**Do not use it when the source changes in bulk, often.**
    The cascade triggers are row-level on purpose: their ``WHEN`` condition lets every unrelated UPDATE of the owner's
    table skip them entirely. The price is that a statement that changes a copied column on *N* owner rows runs the
    trigger function *N* times, and each run is an indexed UPDATE of the leaf rows below that row. That is fine for
    the single-row edits that a user makes. It is the wrong design for a nightly job that renames ten thousand
    ancestors, or for an importer that reparents rows in bulk. If your owners are routinely updated in that way, keep
    the join.

**Be wary of hot, wide fan-out.**
    Renaming an ancestor rewrites every leaf row below it, inside the transaction that renames it, and keeps row locks
    on all of those rows until commit. For an ancestor with a handful of leaf rows, that is invisible. For an ancestor
    with hundreds of thousands of leaf rows below it, it turns one rename into a long-running write. Copy values that
    rarely change, or that have a modest fan-out.

**History tables multiply it.**
    A leaf that is tracked by an audit or history package gets one history row for each copy that the cascade rewrites.
    That is correct, because the row did change, but it is noise in an audit log, and storage that you pay for. See
    :doc:`/guides/exclusions`.


Other costs to know
-------------------

* **Private Django API.** Reading the computed value back after ``save()``, presenting the copy to queries as its
  source column, and inferring a foreign key's target depend on Django internals (``db_returning``, ``get_col``, the
  lazy relation machinery). The test suite runs against every Django minor version that the package supports, and a
  future Django may need a release of this package before you can upgrade.
* **Copies are a second truth.** The triggers keep them equal to their source for every write path that PostgreSQL
  sees. A ``DISABLE TRIGGER``, a restored dump without triggers, or a replication setup that skips triggers can leave
  copies out of date; see :doc:`/guides/drift`.
* **Concurrent writes need SERIALIZABLE.** Below ``SERIALIZABLE``, a leaf write that runs at the same time as an update
  of its ancestor can commit an out-of-date copy. At ``SERIALIZABLE``, one of the two transactions fails with a
  serialization error instead, and the application has to retry it. See
  :ref:`Concurrent writes <concurrent_writes>`.


A rule of thumb
---------------

Copy a value when **an index or a constraint needs it**, or when **most reads filter or sort by it across three or more
tables**, and when it **changes rarely, one row at a time**. Otherwise, join.
