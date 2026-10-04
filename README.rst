===========================
django-postgres-denormalize
===========================

Columns that hold a copy of a value further along a chain of foreign keys, kept equal to it by PostgreSQL triggers, so
indexes, sorting, filtering and unique constraints can cover values that otherwise sit several joins away.

Development
-----------

.. code-block:: bash

    make venv        # create .venv and install the package with its dev extras
    make databases   # start PostgreSQL 17 and 18 in containers
    make test        # run the suite against every supported Django and PostgreSQL version
