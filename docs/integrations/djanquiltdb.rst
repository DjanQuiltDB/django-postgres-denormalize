===========
DjanQuiltDB
===========

This library was written in conjunction with `DjanQuiltDB <https://github.com/djanquiltdb/djanquiltdb>`_, which shards a
Django project across PostgreSQL schemas and nodes. Both libraries work on their own: using django-postgres-denormalize
does not need DjanQuiltDB, or any familiarity with it.

In a sharded project, the copies have to be checked and corrected in each shard, and a lookup path has to stay inside
the shard. DjanQuiltDB's ``postgres-denormalize`` extra installs a plugin with helpers that find and correct out-of-date
copies in one shard or in every shard. DjanQuiltDB's own system checks report a lookup path whose cascade trigger would
write across shards::

    pip install djanquiltdb[postgres-denormalize]

See the `plugin's documentation
<https://djanquiltdb.readthedocs.io/en/latest/plugins/postgres-denormalize/index.html>`_ for how to use it.
