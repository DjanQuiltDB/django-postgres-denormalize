========
Settings
========

Every setting lives in the ``POSTGRES_DENORMALIZE`` dict, and every key is optional:

.. code-block:: python

    # settings.py
    POSTGRES_DENORMALIZE = {
        'EXCLUDE_MODELS': ['tenancy.TaskAudit'],
        'REDUNDANT_COPY_CHECK': {'LEVEL': 'error'},
        'ORDERING_CHECK_LEVEL': 'error',
    }

The setting is validated when the project starts. A value that is not a dict, an unknown key, and a value that this
package cannot use raise ``ImproperlyConfigured``. The values are read each time that they are needed, so
``override_settings`` in a test takes effect.


EXCLUDE_MODELS
--------------

A list of model labels, ``'app_label.ModelName'``, matched case-insensitively. This package skips the models that it
lists: they get no triggers, no drift filter, no backfill and no project checks. By default it lists nothing. An entry
that is not a string with exactly one dot raises ``ImproperlyConfigured``, and an entry that names no installed model is
reported by ``postgres_denormalize.E401``. See :doc:`/guides/exclusions`.


EXCLUDE_MODELS_CALLBACK
-----------------------

A callable that takes a model class and returns ``True`` for a model to exclude, or a dotted path to such a callable.
Use it when the models to exclude follow a rule, and not a list. A model is excluded when either this key or
``EXCLUDE_MODELS`` says so. It defaults to ``None``. A dotted path that cannot be imported, and a value that is not
callable, raise ``ImproperlyConfigured``. See :doc:`/guides/exclusions` for what the callback is called with.


REDUNDANT_COPY_CHECK
--------------------

The redundant copy check reports a plain foreign key whose target the model can also reach through another chain of
foreign keys (see :doc:`/guides/fields`). This key is a dict, and a project sets only the keys that it changes:

.. code-block:: python

    # settings.py
    POSTGRES_DENORMALIZE = {
        'REDUNDANT_COPY_CHECK': {
            'LEVEL': 'warning',
            'APPS': None,
            'ALLOWED': {
                'app_label.Model.field': 'Why this foreign key may lead to a different row than the other route.',
            },
            'MAX_DEPTH': 4,
        },
    }

``LEVEL``
    ``'warning'`` (the default) reports ``postgres_denormalize.W101``, ``'error'`` reports
    ``postgres_denormalize.E101``, and ``'off'`` does not run the check.

``APPS``
    A list of app labels to check. The default, ``None``, checks every app that is not installed in a site-packages
    directory, so the apps of third-party packages are skipped. ``manage.py check <app_label>`` checks only the apps
    that it names. A label that names no installed app is reported by ``postgres_denormalize.E402``.

``ALLOWED``
    A dict that maps a field, ``'app_label.Model.field'``, to the reason why the two routes may lead to different rows.
    The check does not report a field that it lists. An entry whose reason is empty is reported by
    ``postgres_denormalize.E103``, and an entry for a field whose model cannot reach the target in any other way by
    ``postgres_denormalize.E102``. It defaults to an empty dict.

``MAX_DEPTH``
    The number of hops that the check walks from each model, 4 by default. It must be an integer of 2 or more: a
    route of one hop is a second foreign key to the same target, and not a copy, so the shortest route that the check
    reports has two hops.

A key that is not one of these four, an ``APPS`` that is not ``None`` or a list, and an ``ALLOWED`` that is not a dict
raise ``ImproperlyConfigured``.


ORDERING_CHECK_LEVEL
--------------------

The level of the check that reports a ``Meta.ordering`` which walks a lookup path that the model also stores a copy of
(see :doc:`/guides/fields`). ``'warning'`` (the default) reports ``postgres_denormalize.W201``, ``'error'`` reports
``postgres_denormalize.E201``, and ``'off'`` does not run the check.


ISOLATION_CHECK_LEVEL
---------------------

The level of the check that reports a database that stores copies and runs transactions below ``SERIALIZABLE`` (see
:ref:`Concurrent writes <concurrent_writes>`). The check reads ``DATABASES[alias]['OPTIONS']['isolation_level']`` of
every PostgreSQL database to which the router allows the migrations of a model with copies. ``'warning'`` (the default)
reports ``postgres_denormalize.W303``, ``'error'`` reports ``postgres_denormalize.E303``, and ``'off'`` does not run
the check. The check reads only the settings. It does not see a transaction that sets a lower level itself.

