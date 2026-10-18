"""
Context managers for batch updates of the models that a lookup path crosses. They skip the cascade triggers of those
models and correct the copies below them in one set-based pass.

A cascade trigger is row-level. A statement that changes a copied column on *N* rows of an owner therefore runs the
trigger *N* times, and each run is an indexed UPDATE of the leaf rows below one row. Both context managers skip the
cascade triggers on the tables of the named owners. When the block ends, they run ``backfill_denormalized`` for each
leaf that those triggers serve: one UPDATE per denormalized field, which corrects every leaf row that differs from its
lookup path. The block and the correction run in one transaction, so no other transaction sees out-of-date copies. The
block itself does see them: until it ends, every copy below a row that the batch changed has its old value. A query in
the block that reads or filters on such a copy gets a wrong answer, and saving a leaf row in the block does not correct
the row, because the save writes the old copy back unchanged.

The two context managers skip the triggers in different ways:

- ``ignore_cascades`` uses ``pgtrigger.ignore``. It works with any ``DEFERRABLE_CASCADES`` value, but Postgres still
  calls each trigger function once per changed row, and the function returns at once. It skips the triggers only for
  queries that Django sends from the current thread. A statement that is sent through another connection still runs the
  cascade triggers.
- ``defer_cascades`` lists the triggers in a transaction-local Postgres setting that their WHEN condition reads, so
  Postgres does not call the function at all. It skips the triggers for every statement in the transaction, including
  raw SQL. It needs ``DEFERRABLE_CASCADES``, which is on by default.

Neither makes the work on the leaf's side cheaper. The correction reads every row of each leaf to find the rows that
differ, and every row that it rewrites runs the leaf's ``denormalize_set`` trigger, because the UPDATE writes a copy.
Both save the per-row work on the owner's side. That work dominates when a batch changes many owner rows that each have
few leaf rows below them.
"""

import contextlib

import pgtrigger
from django.core.exceptions import ImproperlyConfigured
from django.db import connections, router, transaction

from postgres_denormalize import conf
from postgres_denormalize.operations import backfill_denormalized
from postgres_denormalize.triggers import DEFERRED_CASCADES_SETTING, cascade_triggers_on


def _find_cascade_triggers(owners, using):
    """
    Return the ``(owner, leaf, trigger)`` triples of the cascade triggers on ``owners``, and the database to use.
    """
    if not owners:
        raise TypeError('Name at least one model whose rows the batch updates.')
    found = cascade_triggers_on(owners)
    return found, using or router.db_for_write(found[0][0])


def _correct_leaves(found, using):
    """
    Correct the copies of every leaf that the triggers in ``found`` serve, each leaf once, in label order.
    """
    leaves = {leaf._meta.label: leaf for _owner, leaf, _trigger in found}
    for label in sorted(leaves):
        backfill_denormalized(leaves[label], using=using)


@contextlib.contextmanager
def ignore_cascades(*owners, using=None):
    """
    Skip the cascade triggers on ``owners`` for the queries that Django sends inside the block, through
    ``pgtrigger.ignore``, and correct the copies below them when the block ends.

    The block runs in a transaction, or in a savepoint inside one, on ``using``. By default, that is the database that
    the router chooses for writing the first model in ``owners``. If the block raises, the transaction rolls back and
    nothing is corrected::

        with ignore_cascades(Project):
            Project.objects.filter(tenant=old).update(tenant=new)

    .. warning::

        Until the block ends, every copy below a row that the batch changed has its old value. A query in the block that
        reads or filters on such a copy gets a wrong answer, and saving a leaf row in the block does not correct it. See
        :doc:`/guides/batch_updates`.
    """
    found, using = _find_cascade_triggers(owners, using)
    with transaction.atomic(using=using):
        with pgtrigger.ignore(*(trigger.get_uri(owner) for owner, _leaf, trigger in found), databases=[using]):
            # pgtrigger.ignore runs a query to clear its setting when it ends. The savepoint first rolls back a database
            # error that the block raises, so that this query does not fail on an aborted transaction and hide the
            # error.
            with transaction.atomic(using=using):
                yield
        _correct_leaves(found, using)


@contextlib.contextmanager
def defer_cascades(*owners, using=None):
    """
    Skip the cascade triggers on ``owners`` for every statement inside the block, through their WHEN condition, and
    correct the copies below them when the block ends.

    This needs ``DEFERRABLE_CASCADES``, and raises ``ImproperlyConfigured`` when it is off. ``ignore_cascades`` works
    with either value. The block runs in a transaction on ``using``, as in ``ignore_cascades``. The triggers are listed
    in ``DEFERRED_CASCADES_SETTING`` with ``set_config(..., true)``, and a rollback undoes that together with the rest
    of the block. A nested block adds its triggers to the listed ones and restores the list when it ends. At that point
    it corrects its own leaves. The outer block corrects its leaves when it ends, so a leaf that both blocks serve is
    read twice.

    .. warning::

        Until the block ends, every copy below a row that the batch changed has its old value. A query in the block that
        reads or filters on such a copy gets a wrong answer, and saving a leaf row in the block does not correct it. See
        :doc:`/guides/batch_updates`.
    """
    if not conf.get_setting('DEFERRABLE_CASCADES'):
        raise ImproperlyConfigured(
            f'defer_cascades() needs {conf.SETTING_NAME}["DEFERRABLE_CASCADES"], which is off, so the cascade triggers '
            f'cannot be skipped through their WHEN condition. Use ignore_cascades() instead.'
        )
    found, using = _find_cascade_triggers(owners, using)
    with transaction.atomic(using=using):
        with connections[using].cursor() as cursor:
            cursor.execute('SELECT current_setting(%s, true)', [DEFERRED_CASCADES_SETTING])
            previous = cursor.fetchone()[0] or ''
            listed = [pgid for pgid in previous.split(',') if pgid]
            listed.extend(trigger.get_pgid(owner) for owner, _leaf, trigger in found)
            cursor.execute(
                'SELECT set_config(%s, %s, true)', [DEFERRED_CASCADES_SETTING, ','.join(dict.fromkeys(listed))]
            )
        yield
        with connections[using].cursor() as cursor:
            cursor.execute('SELECT set_config(%s, %s, true)', [DEFERRED_CASCADES_SETTING, previous])
        _correct_leaves(found, using)


__all__ = ['defer_cascades', 'ignore_cascades']
