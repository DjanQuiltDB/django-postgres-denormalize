===============================
Indexing, sorting and filtering
===============================

This is what denormalized columns are for. Once an ancestor's value is a column of the leaf, it is an ordinary column
to PostgreSQL, and everything that only works within one table works with it. Queries should then read through the
copy; see :doc:`/guides/fields`.


Composite indexes
-----------------

With ``Task`` from :ref:`the example models <example_models>`, list a tenant's active tasks, ordered by project name
and title, straight from an index:

.. code-block:: python

    class Task(models.Model):
        ...
        archived = models.BooleanField(default=False)
        tenant = DenormalizedForeignKey('project__tenant')
        project_name = DenormalizedField('project__name')

        class Meta:
            ordering = ['project_name', 'title']
            indexes = [
                models.Index(
                    fields=['tenant', 'project_name', 'title'],
                    condition=models.Q(archived=False),
                    name='task_listing',
                ),
            ]

``Task.objects.filter(tenant=tenant, archived=False)`` is then answered by ``task_listing`` with no join and no sort
step. A partial index whose condition matches the default manager's filter (for example, a soft-delete flag) keeps the
index small.


Unique constraints
------------------

.. code-block:: python

    models.UniqueConstraint(fields=['tenant', 'title'], name='unique_task_title_per_tenant')

The constraint is enforced for every write path, including cascades. Moving a project into a tenant that already has a
task with the same title fails with an ``IntegrityError``, just like inserting such a task does.


Search: trigram and full text
-----------------------------

An expression index over the leaf's own columns makes it possible to search tasks by their project name and title as
one text:

.. code-block:: python

    from django.contrib.postgres.indexes import GinIndex, OpClass
    from django.db.models.functions import Concat, Upper

    indexes = [
        GinIndex(
            OpClass(Upper(Concat('project_name', models.Value(' '), 'title')), name='gin_trgm_ops'),
            name='task_search_trgm',
        ),
    ]

Two things matter:

* **The filter must use the index's own expression.** PostgreSQL serves a filter from an expression index only when the
  filter's expression is the indexed expression. Otherwise it reads every row, without a warning. Build the index and
  the filter from one helper function, so that they always use the same expression. ``icontains`` compiles to
  ``UPPER(col::text) LIKE UPPER(...)``, which is why the example indexes ``Upper(...)``.
* ``OpClass`` needs ``django.contrib.postgres`` in ``INSTALLED_APPS``, and ``gin_trgm_ops`` needs the ``pg_trgm``
  extension.

Because a ``DenormalizedField`` presents itself to queries as its source column's field class, ``Concat`` needs no
``output_field`` and lookups that ``django.contrib.postgres`` registers on ``CharField`` (``trigram_similar``,
``unaccent``, ``search``) apply to a copied ``CharField`` too.


Prove the index is used
-----------------------

Write a test that runs ``EXPLAIN`` on the query that an index is for, with sequential scans (and, for an ordered
listing, sorts) switched off, and asserts that the plan names the index. On an empty or tiny table, the planner may pick
any index, so insert a few realistic rows and run ``ANALYZE`` first:

.. code-block:: python

    class ListingIndexTestCase(TestCase):
        def setUp(self):
            ...  # a few dozen tasks across several projects
            with connection.cursor() as cursor:
                cursor.execute('ANALYZE tenancy_task')
                cursor.execute('SET LOCAL enable_seqscan = off')
                cursor.execute('SET LOCAL enable_sort = off')

        def test_the_listing_reads_the_index(self):
            plan = Task.objects.filter(tenant=self.tenant, archived=False).values('id').explain()

            self.assertIn('task_listing', plan)

Without such a test, a later change to the query or to the index can make PostgreSQL stop using the index, without
any warning.
