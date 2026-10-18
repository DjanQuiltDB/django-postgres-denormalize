django-postgres-denormalize
===========================
Columns that store a copy of a value at the end of a chain of foreign keys, such as the tenant that a row belongs to
through its parent, or the parent's name. PostgreSQL triggers keep the copy equal to the value, so indexes, sorting,
filtering and unique constraints can cover values that are otherwise several joins away.

Release |version|.


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
