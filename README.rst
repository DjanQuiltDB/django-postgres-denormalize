===========================
django-postgres-denormalize
===========================

.. image:: https://img.shields.io/pypi/v/django-postgres-denormalize.svg
    :target: https://pypi.org/project/django-postgres-denormalize/
    :alt: PyPI

.. image:: https://img.shields.io/pypi/pyversions/django-postgres-denormalize.svg
    :target: https://pypi.org/project/django-postgres-denormalize/
    :alt: Supported Python versions

.. image:: https://img.shields.io/pypi/frameworkversions/django/django-postgres-denormalize.svg
    :target: https://pypi.org/project/django-postgres-denormalize/
    :alt: Supported Django versions

.. image:: https://img.shields.io/badge/postgres-17%20%7C%2018-4169e1?logo=postgresql&logoColor=white
    :target: https://www.postgresql.org/
    :alt: Supported PostgreSQL versions

.. image:: https://github.com/djanquiltdb/django-postgres-denormalize/actions/workflows/ci.yml/badge.svg
    :target: https://github.com/djanquiltdb/django-postgres-denormalize/actions/workflows/ci.yml
    :alt: CI

.. image:: https://readthedocs.org/projects/django-postgres-denormalize/badge/?version=latest
    :target: https://django-postgres-denormalize.readthedocs.io/
    :alt: Documentation

.. image:: https://img.shields.io/pypi/l/django-postgres-denormalize.svg
    :target: https://github.com/djanquiltdb/django-postgres-denormalize/blob/master/LICENSE
    :alt: BSD-3-Clause licence

PostgreSQL cannot index, sort or constrain on a value that is stored in another table. A value that is several joins
away, such as the tenant that a row belongs to through its parent, cannot be part of an index or a unique constraint on
the row's table, and every query that filters or orders by it pays for the joins.

This package lets a model store a copy of such a value in an ordinary column, and keeps the copy correct with
PostgreSQL triggers. That works for every write, whether it comes from the ORM, a bulk update, raw SQL or the previous
release that is still serving during a rolling deploy. The model declares the lookup path and nothing else;
``makemigrations`` writes the column, the triggers and their changes.

Full documentation is at https://django-postgres-denormalize.readthedocs.io/.

.. code-block:: python

    from django.db import models
    from postgres_denormalize.fields import DenormalizedField, DenormalizedForeignKey


    class Task(models.Model):
        project = models.ForeignKey('Project', on_delete=models.CASCADE)
        title = models.CharField(max_length=100)
        # A copy of project.tenant: a real foreign key, its target and on_delete rule inferred from the path.
        tenant = DenormalizedForeignKey('project__tenant')
        # A copy of project.name: a column of the same type as the source.
        project_name = DenormalizedField('project__name')

        class Meta:
            ordering = ['project_name', 'title']
            constraints = [models.UniqueConstraint(fields=['tenant', 'title'], name='unique_task_title_per_tenant')]
            indexes = [models.Index(fields=['tenant', 'project_name', 'title'], name='task_listing')]

From then on, saving a task computes its copies in the database and reads them back onto the instance. Moving a
project to another tenant, or renaming it, rewrites the copies of its tasks in the same transaction, but only the
copies that change.

**Use it for** values that an index or a unique constraint needs, and values that most queries sort or filter by across
several joins. **Do not use it** to save a join for rendering, because ``select_related`` does that for free. Do not use
it either for values whose source is often updated in bulk. See `when to use it, and when not to
<https://django-postgres-denormalize.readthedocs.io/en/latest/getting_started/when_to_use.html>`_, and the
`alternatives <https://django-postgres-denormalize.readthedocs.io/en/latest/getting_started/alternatives.html>`_ page,
which compares it with django-pgconstraints and django-computedfields.

Installation
------------

.. code-block:: bash

    pip install django-postgres-denormalize

.. code-block:: python

    INSTALLED_APPS = [
        # ...
        'pgtrigger',
        'postgres_denormalize',
    ]

Requires PostgreSQL and django-pgtrigger, with pgtrigger's migration support turned on (the default). See the
`settings reference <https://django-postgres-denormalize.readthedocs.io/en/latest/reference/settings.html>`_ for the
options.

Relationship to DjanQuiltDB
---------------------------

While this library was written in conjunction with `DjanQuiltDB <https://github.com/djanquiltdb/djanquiltdb>`_ and the
libraries were designed to work together seamlessly in projects where both are installed, both libraries are written to
be used as standalone libraries as well. For using django-postgres-denormalize, it is not necessary to use or be
familiar with DjanQuiltDB.

If you wish to use DjanQuiltDB features for the copies that django-postgres-denormalize keeps, you can install the
``djanquiltdb[postgres-denormalize]`` extra. For more information, please refer to the DjanQuiltDB documentation.
