django-postgres-denormalize
===========================
Columns that store a copy of a value at the end of a chain of foreign keys, such as the tenant that a row belongs to
through its parent, or the parent's name. PostgreSQL triggers keep the copy equal to the value, so indexes, sorting,
filtering and unique constraints can cover values that are otherwise several joins away.

Release |version|.

.. container:: badges

    .. image:: https://github.com/DjanQuiltDB/django-postgres-denormalize/actions/workflows/ci.yml/badge.svg
        :target: https://github.com/DjanQuiltDB/django-postgres-denormalize/actions/workflows/ci.yml
        :alt: CI

    .. image:: https://img.shields.io/codecov/c/github/DjanQuiltDB/django-postgres-denormalize/master
        :target: https://app.codecov.io/gh/DjanQuiltDB/django-postgres-denormalize
        :alt: Coverage

    .. image:: https://img.shields.io/pypi/v/django-postgres-denormalize.svg
        :target: https://pypi.org/project/django-postgres-denormalize/
        :alt: django-postgres-denormalize on PyPI

    .. image:: https://img.shields.io/pypi/pyversions/django-postgres-denormalize.svg
        :target: https://pypi.org/project/django-postgres-denormalize/
        :alt: Supported Python versions

    .. image:: https://img.shields.io/pypi/frameworkversions/django/django-postgres-denormalize.svg
        :target: https://pypi.org/project/django-postgres-denormalize/
        :alt: Supported Django versions

    .. image:: https://img.shields.io/badge/postgres-17%20%7C%2018-4169e1?logo=postgresql&logoColor=white
        :target: https://www.postgresql.org/
        :alt: Supported PostgreSQL versions

    .. image:: https://img.shields.io/pypi/l/django-postgres-denormalize.svg
        :target: https://github.com/DjanQuiltDB/django-postgres-denormalize/blob/master/LICENSE
        :alt: BSD-3-Clause licence


.. toctree::
    :maxdepth: 2
    :caption: Getting started

    getting_started/overview
    getting_started/when_to_use
    getting_started/alternatives
    getting_started/installation

.. toctree::
    :maxdepth: 2
    :caption: Guides

    guides/fields
    guides/triggers
    guides/migrations
    guides/indexing
    guides/batch_updates
    guides/drift
    guides/exclusions

.. toctree::
    :maxdepth: 2
    :caption: Integrations

    integrations/djanquiltdb

.. toctree::
    :maxdepth: 2
    :caption: Reference

    reference/settings
    reference/checks
    reference/api
    reference/changelog


Indices and tables
------------------

* :ref:`genindex`
* :ref:`modindex`
* :ref:`search`
