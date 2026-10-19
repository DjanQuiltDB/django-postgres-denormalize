=============
System checks
=============

``manage.py check`` runs these, and so do ``runserver`` and ``migrate``. Each one reports a mistake that would otherwise
surface late, at migrate time or at the first write, or never.


Field checks
------------

Each denormalized field checks its own declaration:

================================ =======================================================================================
``postgres_denormalize.E001``    The lookup path does not resolve: a segment names no field, is not a forward foreign
                                 key or one-to-one field, or points at a model that is not installed, or the path is a
                                 column of the leaf itself. See :doc:`/guides/fields`.
``postgres_denormalize.E002``    The first hop is nullable, so a leaf without it has nothing to copy from. See
                                 :doc:`/guides/fields`.
``postgres_denormalize.E003``    A ``DenormalizedForeignKey`` has an explicit ``to`` that differs from the model at the
                                 end of its path, or an explicit ``to_field`` that differs from the column that the
                                 last hop points at. See :doc:`/guides/fields`.
``postgres_denormalize.E004``    A ``DenormalizedForeignKey`` crosses a nullable hop but is declared ``null=False``.
                                 See :doc:`/guides/fields`.
``postgres_denormalize.E005``    A ``DenormalizedField`` ends at a foreign key, a primary key or a generated column.
                                 See :doc:`/guides/fields`.
``postgres_denormalize.E006``    The lookup path crosses or ends at another denormalized field. The hint names the path
                                 that copies from the source directly. See :doc:`/guides/fields`.
``postgres_denormalize.E007``    The lookup path crosses a field that a model inherits from a concrete parent, whose
                                 column is in the parent's table. The hint names the path through the parent link. See
                                 :doc:`/guides/fields`.
``postgres_denormalize.E008``    A ``DenormalizedForeignKey`` without ``on_delete`` crosses a hop with a custom
                                 ``on_delete`` rule, so the field cannot infer its own rule. See :doc:`/guides/fields`.
``postgres_denormalize.W009``    A ``DenormalizedForeignKey`` declares an ``on_delete`` rule that differs from the rule
                                 that its path implies. Add it to ``SILENCED_SYSTEM_CHECKS`` to accept the difference.
                                 See :doc:`/guides/fields`.
================================ =======================================================================================

A field on an excluded model (see :doc:`/guides/exclusions`) skips the checks that ``EXCLUDE_MODELS_SILENCED_CHECKS``
lists. By default, these are ``E002``, ``E003``, ``E004`` and ``W009``, which compare the declaration with the lookup
path. No trigger writes the column of an excluded model, so they say nothing useful there. The field still infers its
target, column type and ``on_delete`` rule from the path, so the other checks still run. See
:doc:`/reference/settings`.


Project checks
--------------

These checks are registered under the ``postgres_denormalize`` tag, so ``manage.py check --tag postgres_denormalize``
runs only them. They skip excluded models.

================================ =======================================================================================
``postgres_denormalize.E301``    pgtrigger neither writes triggers into migrations nor installs them after ``migrate``,
                                 so the triggers never reach the database. See :doc:`/getting_started/installation`.
``postgres_denormalize.W302``    A lookup path reads a column of a model from an installed package, so that model gets
                                 the cascade and fill triggers, and ``makemigrations`` writes them into the package's
                                 migrations. Point the foreign key at a proxy model in one of your own apps. See
                                 :doc:`/guides/triggers`.
``postgres_denormalize.W303``    A database that stores copies runs transactions below ``SERIALIZABLE``, so concurrent
                                 writes can commit an out-of-date copy. It is ``E303`` when ``ISOLATION_CHECK_LEVEL``
                                 is ``'error'``. See :ref:`Concurrent writes <concurrent_writes>`.
``postgres_denormalize.W101``    A plain foreign key whose target the model can also reach through another chain of
                                 foreign keys, so the column stores a copy that no trigger keeps equal to its source.
                                 It is ``E101`` when ``REDUNDANT_COPY_CHECK['LEVEL']`` is ``'error'``. See
                                 :doc:`/guides/fields`.
``postgres_denormalize.E102``    A ``REDUNDANT_COPY_CHECK['ALLOWED']`` entry names a field whose model cannot reach the
                                 target in any other way, so the entry is not needed. See :doc:`/reference/settings`.
``postgres_denormalize.E103``    A ``REDUNDANT_COPY_CHECK['ALLOWED']`` entry has an empty reason. See
                                 :doc:`/reference/settings`.
``postgres_denormalize.W201``    A ``Meta.ordering`` walks a lookup path that the model also stores a copy of. Every
                                 query with the default ordering then joins the tables that the copy is meant to avoid,
                                 and an index on the copy cannot serve it. It is ``E201`` when
                                 ``ORDERING_CHECK_LEVEL`` is ``'error'``. See :doc:`/guides/fields`.
``postgres_denormalize.E401``    An ``EXCLUDE_MODELS`` entry names no installed model, so it excludes nothing. This is
                                 usually a typo or a renamed model. See :doc:`/guides/exclusions`.
``postgres_denormalize.E402``    A ``REDUNDANT_COPY_CHECK['APPS']`` entry names no installed app, so it checks nothing.
                                 See :doc:`/reference/settings`.
================================ =======================================================================================

Warnings never block anything. A project that wants them enforced sets ``REDUNDANT_COPY_CHECK['LEVEL']``,
``ORDERING_CHECK_LEVEL`` and ``ISOLATION_CHECK_LEVEL`` to ``'error'``, and ``'off'`` turns each of these checks off
(see :doc:`/reference/settings`). With
errors, Django refuses to run ``migrate``, ``runserver`` or the test suite until they are fixed. That is safe for a team
whose CI runs the same checks before every deploy.
