========
Overview
========

A relational schema stores each fact once. Take a task tracker in which a tenant has projects, a project has tasks,
and a task has comments. The tenant of a task is found by walking ``task.project.tenant``. That is right for keeping
facts consistent, and it has a price whenever the database has to *use* the tenant of a task:

* **Indexes cannot reach across a join.** A PostgreSQL index, an expression index included, reads the row's own table.
  An index on ``(tenant, project name, title)`` for listing a tenant's tasks cannot exist while the tenant and the
  project name live in other tables.
* **Unique constraints cannot reach across a join.** "A task title is unique within its tenant" cannot be a
  ``UniqueConstraint`` on the tasks table.
* **Sorting and filtering pay for every join** on every query: a list of comments ordered by project name joins three
  tables for each page.

This package lets a model store a **copy** of such a value in an ordinary column, and keeps the copy correct with
PostgreSQL triggers.


.. _example_models:

The example models
------------------

The task tracker, in an app labelled ``tenancy``. ``Task`` and ``Comment`` declare copies:

.. code-block:: python

    # tenancy/models.py
    from django.db import models
    from postgres_denormalize.fields import DenormalizedField, DenormalizedForeignKey


    class Tenant(models.Model):
        name = models.CharField(max_length=100)


    class Project(models.Model):
        tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE)
        name = models.CharField(max_length=100)


    class Task(models.Model):
        project = models.ForeignKey(Project, on_delete=models.CASCADE)
        title = models.CharField(max_length=100)
        tenant = DenormalizedForeignKey('project__tenant')
        project_name = DenormalizedField('project__name')

        class Meta:
            constraints = [models.UniqueConstraint(fields=['tenant', 'title'], name='unique_task_title_per_tenant')]
            indexes = [models.Index(fields=['tenant', 'project_name', 'title'], name='task_listing')]


    class Comment(models.Model):
        task = models.ForeignKey(Task, on_delete=models.CASCADE)
        tenant = DenormalizedForeignKey('task__project__tenant')

The code examples on the other pages use these models. The table and trigger names in those examples, such as
``tenancy_task``, come from the app label.

The models declare the lookup paths and nothing else. ``makemigrations`` writes the columns, the triggers and the
index, and ``migrate`` installs them. From then on:

* every INSERT and UPDATE of a task computes its copies in the database, no matter what writes the row: the ORM, a bulk
  load, raw SQL, or the previous release that is still serving during a rolling deployment;
* moving a project to another tenant, or renaming it, rewrites the copies of every task below it, in the same
  transaction;
* the instance has the computed values after ``save()``, without a reload.


Terms
-----

The documentation uses a few words consistently:

**Leaf**
    The model that declares the denormalized field (``Task``).

**Lookup path**
    The field's ``denormalize_from`` (``'project__tenant'``): a chain of forward foreign keys or one-to-one fields that
    starts on the leaf and ends at the column whose value is copied.

**Hop**
    Each foreign key or one-to-one field along the lookup path. The first hop (``project``) is a column of the leaf
    itself.

**Ancestor**
    A row that the lookup path passes through: a task's project, and that project's tenant.

**Source column**
    The last segment of the path: a foreign key for a ``DenormalizedForeignKey`` (``tenant``), any other column for a
    ``DenormalizedField`` (``name``).

**Owner**
    A model on which a segment after the first is declared (``Project`` declares ``tenant`` and ``name``). Owners have
    the triggers that push changes down to the leaves.


What it does not do
-------------------

It copies, and it does not compute. A copy is the value of one column at the end of a chain of forward foreign keys
and one-to-one fields. A value that is calculated from several columns, or that is an aggregate over reverse or
many-to-many relations, is out of scope; :doc:`/getting_started/alternatives` names the tools that do that.

It works only on PostgreSQL. The triggers are PL/pgSQL, and there is no fallback for other databases.

It does not repair a copy that is out of date because of a write that the triggers do not see, such as a write while
the triggers are disabled.
:doc:`/guides/drift` describes how to find and correct such rows.

Before you adopt it, read :doc:`/getting_started/when_to_use`. A copy adds a real cost to every write, and for some
workloads it is the wrong tool.
