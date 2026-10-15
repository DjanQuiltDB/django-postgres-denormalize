"""
The ``POSTGRES_DENORMALIZE`` setting: its defaults, its validation, and the functions through which the rest of the
package reads it.

Every key is optional::

    POSTGRES_DENORMALIZE = {
        # Models that this package skips: no triggers, no drift filter, no backfill, no project checks. This is for
        # models that have denormalized fields on purpose without keeping them in sync, such as audit tables. A model is
        # excluded when either key says so.
        'EXCLUDE_MODELS': ['tenancy.TaskAudit'],  # model labels, matched case-insensitively
        'EXCLUDE_MODELS_CALLBACK': 'myproject.audit.is_audit_model',  # a callable, or a dotted path to one
        'REDUNDANT_COPY_CHECK': {
            'LEVEL': 'warning',  # or 'error', or 'off'
            'APPS': None,  # app labels to check; None checks every app outside site-packages
            'ALLOWED': {'app_label.Model.field': 'why this foreign key may differ from the other route'},
            'MAX_DEPTH': 4,
        },
        'ORDERING_CHECK_LEVEL': 'warning',  # or 'error', or 'off'
        # The level of the check that each database that stores copies runs transactions at SERIALIZABLE.
        'ISOLATION_CHECK_LEVEL': 'warning',  # or 'error', or 'off'
    }

The values are read each time they are needed, not once at start-up, so ``override_settings`` in a test takes effect.
"""

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.utils.module_loading import import_string

SETTING_NAME = 'POSTGRES_DENORMALIZE'

LEVELS = ('off', 'warning', 'error')

DEFAULT_REDUNDANT_COPY_CHECK = {
    'LEVEL': 'warning',
    'APPS': None,
    'ALLOWED': {},
    'MAX_DEPTH': 4,
}

DEFAULTS = {
    'EXCLUDE_MODELS': (),
    'EXCLUDE_MODELS_CALLBACK': None,
    'REDUNDANT_COPY_CHECK': DEFAULT_REDUNDANT_COPY_CHECK,
    'ORDERING_CHECK_LEVEL': 'warning',
    'ISOLATION_CHECK_LEVEL': 'warning',
}


def user_settings():
    """
    Return the project's ``POSTGRES_DENORMALIZE`` dict, empty when the project sets none.
    """
    return getattr(settings, SETTING_NAME, None) or {}


def get_setting(key):
    """
    Return the value of one key, or its default when the project does not set it. ``REDUNDANT_COPY_CHECK`` is merged
    key by key, so a project can set only ``LEVEL``.
    """
    value = user_settings().get(key, DEFAULTS[key])
    if key == 'REDUNDANT_COPY_CHECK':
        return {**DEFAULT_REDUNDANT_COPY_CHECK, **value}
    return value


def excluded_labels():
    """
    Return the model labels in ``EXCLUDE_MODELS``, in lowercase.
    """
    return {label.lower() for label in get_setting('EXCLUDE_MODELS')}


def exclusion_callback():
    """
    Return the callable that ``EXCLUDE_MODELS_CALLBACK`` names, or None. A dotted path is imported.
    """
    callback = get_setting('EXCLUDE_MODELS_CALLBACK')
    if isinstance(callback, str):
        return import_string(callback)
    return callback


def is_excluded(model):
    """
    Say whether this package skips ``model``: either ``EXCLUDE_MODELS`` lists it, or ``EXCLUDE_MODELS_CALLBACK`` returns
    a true value for it. When neither is configured, no model is excluded.
    """
    if model._meta.label_lower in excluded_labels():
        return True
    callback = exclusion_callback()
    return callback is not None and bool(callback(model))


def validate_settings():
    """
    Raise ``ImproperlyConfigured`` for a ``POSTGRES_DENORMALIZE`` setting that this package cannot use.

    ``PostgresDenormalizeConfig.ready()`` calls this, so a misspelt key or level stops the project from starting and is
    not ignored.
    """
    configured = user_settings()
    if not isinstance(configured, dict):
        raise ImproperlyConfigured(f'{SETTING_NAME} must be a dict, not {type(configured).__name__}.')

    unknown = sorted(set(configured) - set(DEFAULTS))
    if unknown:
        raise ImproperlyConfigured(f'{SETTING_NAME} has unknown keys: {", ".join(unknown)}.')

    labels = get_setting('EXCLUDE_MODELS')
    if isinstance(labels, str) or not isinstance(labels, (list, tuple, set, frozenset)):
        raise ImproperlyConfigured(f'{SETTING_NAME}["EXCLUDE_MODELS"] must be a list of model labels.')
    malformed = sorted(label for label in labels if not isinstance(label, str) or label.count('.') != 1)
    if malformed:
        raise ImproperlyConfigured(
            f'{SETTING_NAME}["EXCLUDE_MODELS"] contains {", ".join(map(repr, malformed))}, but each entry must be a '
            f"model label such as 'app_label.ModelName'."
        )

    callback = get_setting('EXCLUDE_MODELS_CALLBACK')
    if isinstance(callback, str):
        try:
            import_string(callback)
        except ImportError as exception:
            raise ImproperlyConfigured(
                f'{SETTING_NAME}["EXCLUDE_MODELS_CALLBACK"] is {callback!r}, which cannot be imported: {exception}'
            ) from exception
    elif callback is not None and not callable(callback):
        raise ImproperlyConfigured(
            f'{SETTING_NAME}["EXCLUDE_MODELS_CALLBACK"] must be a callable or a dotted path to one.'
        )

    redundant = configured.get('REDUNDANT_COPY_CHECK', {})
    if not isinstance(redundant, dict):
        raise ImproperlyConfigured(f'{SETTING_NAME}["REDUNDANT_COPY_CHECK"] must be a dict.')
    unknown = sorted(set(redundant) - set(DEFAULT_REDUNDANT_COPY_CHECK))
    if unknown:
        raise ImproperlyConfigured(f'{SETTING_NAME}["REDUNDANT_COPY_CHECK"] has unknown keys: {", ".join(unknown)}.')

    merged = get_setting('REDUNDANT_COPY_CHECK')
    _validate_level('REDUNDANT_COPY_CHECK"]["LEVEL', merged['LEVEL'])
    if merged['APPS'] is not None and not isinstance(merged['APPS'], (list, tuple, set)):
        raise ImproperlyConfigured(f'{SETTING_NAME}["REDUNDANT_COPY_CHECK"]["APPS"] must be None or a list of labels.')
    if not isinstance(merged['ALLOWED'], dict):
        raise ImproperlyConfigured(f'{SETTING_NAME}["REDUNDANT_COPY_CHECK"]["ALLOWED"] must be a dict.')
    if not isinstance(merged['MAX_DEPTH'], int) or merged['MAX_DEPTH'] < 2:
        raise ImproperlyConfigured(
            f'{SETTING_NAME}["REDUNDANT_COPY_CHECK"]["MAX_DEPTH"] must be an integer of 2 or more.'
        )

    _validate_level('ORDERING_CHECK_LEVEL', get_setting('ORDERING_CHECK_LEVEL'))
    _validate_level('ISOLATION_CHECK_LEVEL', get_setting('ISOLATION_CHECK_LEVEL'))


def _validate_level(key, value):
    if value not in LEVELS:
        raise ImproperlyConfigured(f'{SETTING_NAME}["{key}"] is {value!r}; use one of {", ".join(LEVELS)}.')


__all__ = ['DEFAULTS', 'LEVELS', 'get_setting', 'is_excluded', 'validate_settings']
