============
Installation
============

.. code-block:: shell

    pip install django-postgres-denormalize

Add it to ``INSTALLED_APPS``, after ``pgtrigger``:

.. code-block:: python

    INSTALLED_APPS = [
        # ...
        'pgtrigger',
        'postgres_denormalize',
        # your apps
    ]

Leave django-pgtrigger's ``PGTRIGGER_MIGRATIONS`` at its default (on). The triggers that this package derives reach
the database through migrations, so they also work with schema-per-tenant setups that migrate every schema. A project
that turns pgtrigger's migrations off has to turn ``PGTRIGGER_INSTALL_ON_MIGRATE`` on, and the system check
``postgres_denormalize.E301`` reports a configuration in which both are off.

Every key of the ``POSTGRES_DENORMALIZE`` setting is optional. Decide on ``DEFERRABLE_CASCADES`` (on by default) before
the first ``makemigrations``: it is compiled into every cascade trigger, so changing it later generates a migration that
recreates all of them.

See :doc:`/reference/settings` for every key, and :doc:`/guides/fields` for declaring the first copy. Then run
``makemigrations`` and ``migrate``.


Requirements
------------

* Python 3.14
* Django 6.0 or 6.1
* PostgreSQL 17 or 18
* `django-pgtrigger <https://django-pgtrigger.readthedocs.io/>`_ 4.17 or later, which this package builds on
